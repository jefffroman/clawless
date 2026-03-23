import json, shutil, os
from datetime import datetime

# Path can be overridden via environment variable for server deployments
# where the openclaw user home differs from the provisioning user home.
CONFIG_PATH = os.environ.get(
    "OPENCLAW_CONFIG_PATH",
    os.path.expanduser("~/.openclaw/openclaw.json")
)

# Channel to configure (e.g. "telegram") and its provider-specific config
# blob (JSON string shaped by the storefront for each channel type).
# If either is absent the channels block is left untouched.
CHANNEL = os.environ.get("OPENCLAW_CHANNEL", "").strip().lower()
CHANNEL_CONFIG = json.loads(os.environ.get("OPENCLAW_CHANNEL_CONFIG", "null") or "null")
# Full OpenClaw model string (e.g. "bedrock/us.amazon.nova-micro-v1:0").
# If absent the existing model config is left untouched.
MODEL = os.environ.get("OPENCLAW_MODEL", "").strip()
# Workspace path override — sandbox moves workspace from openclaw user's home
# to the agent user's home directory.
WORKSPACE_PATH = os.environ.get("OPENCLAW_WORKSPACE", "").strip()

MEMORY_SEARCH_BLOCK = {
    "memorySearch": {
        "enabled": True,
        "sources": ["memory", "sessions"],
        "extraPaths": [
            "SOUL.md", "AGENTS.md", "HEARTBEAT.md",
            "PROJECTS.md", "TOOLS.md", "IDENTITY.md",
            "USER.md", "reference/", "ARCHITECTURE.md"
        ],
        "experimental": {"sessionMemory": True}
    }
}

# MCP servers available to the agent.
# transport: stdio is required by the MCP spec for subprocess-based servers.
MCP_SERVERS = {
    "inboxapi": {
        "command": "inboxapi",
        "args": [],
        "transport": "stdio",
    }
}

# Enable full tool access. Without this the agent may boot with no shell/file
# access (the "messaging" profile trap — see openclaw issue #33225).
TOOLS_BLOCK = {"profile": "full"}

# SearXNG web search — local instance, no API key required.
# Default baseUrl assumes sandbox.mode=off (tools run on host, localhost works).
# If sandbox is enabled, switch SEARXNG_HOST to host.docker.internal or 172.17.0.1
# and set sandbox.docker.network to bridge.
# Default to host.docker.internal when sandbox is active (AGENT_UID set),
# since the container can't reach the host's 127.0.0.1 directly.
_default_searxng_host = "host.docker.internal" if os.environ.get("AGENT_UID") else "127.0.0.1"
SEARXNG_HOST = os.environ.get("SEARXNG_HOST", _default_searxng_host)
SEARXNG_PORT = os.environ.get("SEARXNG_PORT", "8080")
WEB_SEARCH_BLOCK = {
    "web": {
        "search": {
            "enabled": True,
            "provider": "searxng",
            "searxng": {"baseUrl": f"http://{SEARXNG_HOST}:{SEARXNG_PORT}"},
        }
    }
}

# Per-peer session isolation: each person who DMs the bot gets their own
# conversation thread. Safe default given dmPolicy: "open" on the Telegram channel.
SESSION_BLOCK = {"dmScope": "per-peer"}

# Sandbox: tools run in a Docker container as the agent UID.
# The gateway (openclaw_user) manages the container; tool execution is isolated.
# host-gateway lets the container reach host services (SearXNG on loopback).
AGENT_UID = os.environ.get("AGENT_UID", "")
SANDBOX_BLOCK = {
    "mode": "docker",
    "docker": {
        "network": "bridge",
        "extraArgs": ["--add-host=host.docker.internal:host-gateway"],
    },
} if AGENT_UID else None

if AGENT_UID:
    SANDBOX_BLOCK["docker"]["user"] = AGENT_UID


def patch_config():
    with open(CONFIG_PATH) as f:
        config = json.load(f)

    backup = CONFIG_PATH + f".bak.{datetime.now().strftime('%Y%m%d%H%M%S')}"
    shutil.copy(CONFIG_PATH, backup)
    print(f"Backed up to {backup}")

    if WORKSPACE_PATH:
        config["workspaceDir"] = WORKSPACE_PATH
        print(f"workspaceDir patched to {WORKSPACE_PATH}")

    config.setdefault("agents", {}).setdefault("defaults", {}).update(MEMORY_SEARCH_BLOCK)

    if MODEL:
        config.setdefault("agents", {}).setdefault("defaults", {}).setdefault("model", {})["primary"] = MODEL
        print(f"agents.defaults.model.primary patched to {MODEL}")

    # Always patch tools and session — these are safe idempotent defaults.
    existing_tools = config.get("tools", {})
    config["tools"] = {**existing_tools, **TOOLS_BLOCK, **WEB_SEARCH_BLOCK}
    print(f"tools patched: profile={TOOLS_BLOCK['profile']}, web.search.provider=searxng")

    existing_session = config.get("session", {})
    config["session"] = {**existing_session, **SESSION_BLOCK}
    print(f"session.dmScope patched to {SESSION_BLOCK['dmScope']}")

    existing_mcp = config.get("mcpServers", {})
    config["mcpServers"] = {**existing_mcp, **MCP_SERVERS}
    print(f"mcpServers patched: {list(MCP_SERVERS.keys())}")

    if SANDBOX_BLOCK:
        existing_sandbox = config.get("sandbox", {})
        config["sandbox"] = {**existing_sandbox, **SANDBOX_BLOCK}
        print(f"sandbox patched: mode=docker, user={AGENT_UID}")

    if CHANNEL and CHANNEL_CONFIG:
        # Merge into any existing channel block so manually-added fields
        # (e.g. allowFrom entries added via pairing) survive re-runs.
        existing = config.setdefault("channels", {}).get(CHANNEL, {})
        config["channels"][CHANNEL] = {**existing, **CHANNEL_CONFIG}
        print(f"channels.{CHANNEL} patched")

    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)
    print("openclaw.json patched — restart OpenClaw to apply")


if __name__ == "__main__":
    patch_config()
