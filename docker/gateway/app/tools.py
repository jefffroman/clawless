"""Built-in tools exposed to Bedrock Converse.

Six tools, no manifest loader, no subprocess plugin model. Memory-curation is
a system-prompt protocol the agent enacts via ``read_file`` / ``write_file``
on its own ``memory/`` directory; it is not a tool here.

Tool contract: each Tool has a JSON schema (Bedrock toolConfig) and an async
``run(input)`` that returns text. ``run`` may raise; callers wrap into a
``toolResult`` content block with ``status: error`` on failure.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import shlex
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import boto3

from .config import Config

log = logging.getLogger("clawless.tools")


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    run: Callable[[dict[str, Any]], Awaitable[str]]

    def as_bedrock_spec(self) -> dict[str, Any]:
        return {
            "toolSpec": {
                "name": self.name,
                "description": self.description,
                "inputSchema": {"json": self.input_schema},
            }
        }


# ---------------------------------------------------------------------------
# Path scoping
# ---------------------------------------------------------------------------


class PathScopeError(ValueError):
    pass


def _resolve_scoped(workspace_dir: str, rel_path: str) -> str:
    """Resolve ``rel_path`` against ``workspace_dir`` and reject escapes.

    Accepts paths relative to workspace root or absolute paths inside it.
    Rejects symlinks that escape the workspace.
    """
    if not rel_path or rel_path.strip() == "":
        raise PathScopeError("path must not be empty")
    if os.path.isabs(rel_path):
        candidate = os.path.realpath(rel_path)
    else:
        candidate = os.path.realpath(os.path.join(workspace_dir, rel_path))
    workspace_real = os.path.realpath(workspace_dir)
    if candidate != workspace_real and not candidate.startswith(workspace_real + os.sep):
        raise PathScopeError(f"path {rel_path!r} escapes workspace {workspace_dir!r}")
    return candidate


# ---------------------------------------------------------------------------
# bash
# ---------------------------------------------------------------------------


async def _run_bash(cfg: Config, args: dict[str, Any]) -> str:
    cmd = args.get("command", "").strip()
    if not cmd:
        return "error: command is required"
    timeout = int(args.get("timeout_s", 60))
    timeout = max(1, min(timeout, 300))

    # Minimal env. Critically: NO AWS credential vars. The bash subshell must
    # not be able to assume the task role (which has Bedrock + S3 +
    # ECS:UpdateService grants) — that path would let the agent restart, stop,
    # or destroy the gateway, exfil credentials, or wipe the workspace
    # bucket. AWS-bound work happens in-process via the built-in tools
    # (sleep, web_search), which run with the gateway's own credential chain.
    # HOME=/tmp because clawless-tool has no home directory.
    env = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": "/tmp",
        "AGENT_SLUG": cfg.agent_slug,
    }

    def _runner() -> tuple[int, str, str]:
        proc = subprocess.run(
            # Run as clawless-tool (UID 1001) via sudo. Different UID from the
            # gateway means: cannot signal the gateway (different-user signals
            # are EPERM), cannot read /proc/<gateway>/environ (kernel blocks
            # cross-UID), cannot write workspace files (gateway owns them at
            # mode 0755). Read access to the workspace cwd works because home
            # is world-readable; writes are routed through the write_file
            # tool which is gated by path scoping.
            ["sudo", "-n", "-u", "clawless-tool", "/usr/bin/bash", "-c", cmd],
            cwd=cfg.workspace_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr

    loop = asyncio.get_running_loop()
    try:
        rc, out, err = await loop.run_in_executor(None, _runner)
    except subprocess.TimeoutExpired:
        return f"error: command timed out after {timeout}s"

    body = []
    if out:
        body.append(out.rstrip())
    if err:
        body.append(f"[stderr]\n{err.rstrip()}")
    body.append(f"[exit_code] {rc}")
    return "\n".join(body)


# ---------------------------------------------------------------------------
# read_file / write_file / list_dir
# ---------------------------------------------------------------------------


async def _run_read_file(cfg: Config, args: dict[str, Any]) -> str:
    rel = args.get("path", "")
    try:
        path = _resolve_scoped(cfg.workspace_dir, rel)
    except PathScopeError as e:
        return f"error: {e}"
    if not os.path.exists(path):
        return f"error: not found: {rel}"
    if os.path.isdir(path):
        return f"error: {rel} is a directory; use list_dir"
    try:
        with open(path) as f:
            return f.read()
    except UnicodeDecodeError:
        return f"error: {rel} is not a text file"
    except OSError as e:
        return f"error: {e}"


async def _run_write_file(cfg: Config, args: dict[str, Any]) -> str:
    rel = args.get("path", "")
    content = args.get("content", "")
    if not isinstance(content, str):
        return "error: content must be a string"
    try:
        path = _resolve_scoped(cfg.workspace_dir, rel)
    except PathScopeError as e:
        return f"error: {e}"
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        f.write(content)
    os.replace(tmp, path)
    return f"wrote {len(content)} chars to {rel}"


async def _run_list_dir(cfg: Config, args: dict[str, Any]) -> str:
    rel = args.get("path", ".")
    try:
        path = _resolve_scoped(cfg.workspace_dir, rel)
    except PathScopeError as e:
        return f"error: {e}"
    if not os.path.isdir(path):
        return f"error: not a directory: {rel}"
    entries = []
    for name in sorted(os.listdir(path)):
        full = os.path.join(path, name)
        if os.path.isdir(full):
            entries.append(f"{name}/")
        else:
            try:
                size = os.path.getsize(full)
                entries.append(f"{name} ({size} bytes)")
            except OSError:
                entries.append(name)
    return "\n".join(entries) if entries else "(empty)"


# ---------------------------------------------------------------------------
# web_search (inline SearXNG, no subprocess)
# ---------------------------------------------------------------------------


async def _run_web_search(cfg: Config, args: dict[str, Any]) -> str:
    if not cfg.searxng_url:
        return "error: SEARXNG_URL not configured"
    query = (args.get("query") or "").strip()
    if not query:
        return "error: query is required"
    n = int(args.get("num", 10))
    n = max(1, min(n, 30))
    category = args.get("category", "general")

    qs = urllib.parse.urlencode({"q": query, "format": "json", "categories": category})
    url = cfg.searxng_url.rstrip("/") + "/search?" + qs

    def _fetch() -> dict[str, Any]:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    loop = asyncio.get_running_loop()
    try:
        payload = await loop.run_in_executor(None, _fetch)
    except Exception as e:
        return f"error: searxng request failed: {e}"

    results = (payload.get("results") or [])[:n]
    if not results:
        return "(no results)"
    lines = []
    for i, r in enumerate(results, 1):
        title = r.get("title") or "(untitled)"
        href = r.get("url") or ""
        snippet = (r.get("content") or "").strip()
        lines.append(f"{i}. {title}\n   {href}\n   {snippet}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# sleep (built-in: SSM /active=false + SFN trigger)
# ---------------------------------------------------------------------------


async def _run_sleep(cfg: Config, args: dict[str, Any]) -> str:
    if not cfg.lifecycle_sfn_arn:
        return "error: LIFECYCLE_SFN_ARN not configured"

    def _go() -> str:
        ssm = boto3.client("ssm", region_name=cfg.aws_region)
        sfn = boto3.client("stepfunctions", region_name=cfg.aws_region)
        ssm.put_parameter(
            Name=f"/clawless/clients/{cfg.agent_slug}/active",
            Type="String",
            Value="false",
            Overwrite=True,
        )
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        sfn.start_execution(
            stateMachineArn=cfg.lifecycle_sfn_arn,
            input=json.dumps({
                "name": f"/clawless/clients/{cfg.agent_slug}",
                "operation": "Update",
                "time": now_iso,
            }),
        )
        return "sleep requested; expect SIGTERM shortly"

    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, _go)
    except Exception as e:
        log.exception("sleep tool failed")
        return f"error: sleep request failed: {e}"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def build_registry(cfg: Config) -> dict[str, Tool]:
    tools = [
        Tool(
            name="bash",
            description=(
                "Run a shell command inside the agent's workspace. "
                "stdout, stderr, and exit code are returned."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run."},
                    "timeout_s": {"type": "integer", "description": "Timeout in seconds (1-300; default 60)."},
                },
                "required": ["command"],
            },
            run=functools.partial(_run_bash, cfg),
        ),
        Tool(
            name="read_file",
            description=(
                "Read a UTF-8 text file from the agent's workspace. Path is "
                "relative to WORKSPACE_DIR; absolute paths are rejected unless "
                "they resolve inside the workspace."
            ),
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            run=functools.partial(_run_read_file, cfg),
        ),
        Tool(
            name="write_file",
            description=(
                "Atomically write content to a path inside the agent's workspace. "
                "Creates parent directories. Overwrites existing files."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
            run=functools.partial(_run_write_file, cfg),
        ),
        Tool(
            name="list_dir",
            description="List the entries of a directory inside the agent's workspace.",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Directory path; defaults to '.'"}},
                "required": [],
            },
            run=functools.partial(_run_list_dir, cfg),
        ),
        Tool(
            name="web_search",
            description=(
                "Search the public web via the shared SearXNG endpoint. "
                "Returns up to N results with title, URL, and snippet."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "num": {"type": "integer", "description": "Number of results (1-30; default 10)."},
                    "category": {
                        "type": "string",
                        "description": "SearXNG category (general, news, images, videos, science, ...).",
                    },
                },
                "required": ["query"],
            },
            run=functools.partial(_run_web_search, cfg),
        ),
        Tool(
            name="sleep",
            description=(
                "Put the agent to sleep. Persists workspace to S3 on SIGTERM and "
                "scales the Fargate service to zero. Use when the user asks to "
                "pause, sleep, shut down, or stop. The agent will wake automatically "
                "on the next inbound message via the wake listener."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "Optional human-readable reason."},
                },
                "required": [],
            },
            run=functools.partial(_run_sleep, cfg),
        ),
    ]
    return {t.name: t for t in tools}


def bedrock_tool_config(registry: dict[str, Tool]) -> dict[str, Any]:
    return {"tools": [t.as_bedrock_spec() for t in registry.values()]}
