"""Pre-event memory flush — durable-knowledge capture before lossy events.

A flush is a one-shot agent turn against the primary model, with the full
tool registry, asking the agent to capture durable knowledge into
``memory/YYYY-MM-DD.md`` via ``write_file``. The flush turn's reply text is
discarded — the side effect on disk is what we want; the next reindex picks
it up.

Two public entrypoints:

* ``run_memory_flush(...)`` — runs one flush turn over a caller-supplied
  ``turns`` window. The caller is responsible for filtering to an
  incremental window (turns since last flush) before calling.

* ``flush_then_reindex(...)`` — convenience helper that filters by
  ``since_ts``, runs the flush, then reindexes. Returns the ts of the
  newest evaluated turn so the caller can advance its high-water mark,
  or ``None`` if nothing was flushed.

This module never decides whether to flush. The trigger predicates (compaction
threshold, periodic-growth in the maintenance loop, hard-ceiling) live in
their respective callers.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .bedrock import BedrockClient
from .memory import MemoryIndex
from .tools import Tool
from .transcript import Turn

log = logging.getLogger("clawless.memory_flush")


def today_iso_date(tz_name: str | None = None) -> str:
    """The date used for daily-note filenames.

    ``tz_name`` is an IANA zone (e.g. ``"America/New_York"``); ``None`` falls
    back to UTC. Aligning the day boundary to the operator's locale keeps
    cron / calendar / log timing consistent — UTC and local can otherwise
    differ by hours.
    """
    tz = ZoneInfo(tz_name) if tz_name else timezone.utc
    return datetime.now(tz).strftime("%Y-%m-%d")


def _flush_prompt(today: str) -> str:
    return (
        "Memory flush.\n\n"
        "Look at the recent portion of this conversation and capture any "
        "durable knowledge worth persisting across sessions. Use the "
        "`write_file` tool ONCE with "
        f"`path=\"memory/{today}.md\"` to add bullet-style notes to today's "
        "memory file. If the file already has content, read it first with "
        "`read_file` and append your new notes at the end. Bundle all your "
        "observations into a single `write_file` call.\n\n"
        "Rules:\n"
        "- Treat top-level files (MEMORY.md, SOUL.md, AGENTS.md, IDENTITY.md, "
        "USER.md, etc.) as read-only. Do not touch them.\n"
        "- Do not create timestamped variants like `memory/YYYY-MM-DD-foo.md`. "
        "Those are journal-only and not indexed by the memory system.\n"
        "- Preserve only durable signal: decisions, facts confirmed, "
        "commitments, surprises, repeated patterns. Skip ephemera "
        "(in-progress chatter, search noise, raw tool outputs).\n"
        "- If nothing meets the bar, do nothing — silence is fine.\n\n"
        "Reply briefly when done."
    )


async def run_memory_flush(
    *,
    bedrock: BedrockClient,
    sid: str,
    turns: list[Turn],
    primary_model_id: str,
    tools: dict[str, Tool],
    tool_config: dict[str, Any],
    system_block: list[dict[str, Any]],
    reason: str,
    tz_name: str | None = None,
) -> bool:
    """Run one flush turn against the supplied ``turns`` window. The caller
    has already filtered ``turns`` to the incremental window. The flush
    turn's reply text is discarded; the side effect we want is files on
    disk.

    Returns True on a successful run (regardless of whether the agent
    actually wrote anything — silence is allowed). Returns False on error.

    ``reason`` is a short label ("pre-compact" / "periodic-growth") logged
    when the flush starts.
    """
    log.info("[%s] memory flush starting (reason=%s, %d turns)",
             sid, reason, len(turns))
    history = [t.as_message() for t in turns]
    history.append({
        "role": "user",
        "content": [{"text": _flush_prompt(today_iso_date(tz_name))}],
    })
    try:
        await bedrock.run_turn(
            model_id=primary_model_id,
            history=history,
            system=system_block,
            tools=tools,
            tool_config=tool_config,
        )
    except Exception:
        log.exception("[%s] memory flush turn failed (reason=%s)", sid, reason)
        return False
    log.info("[%s] memory flush done (reason=%s)", sid, reason)
    return True


async def flush_then_reindex(
    *,
    bedrock: BedrockClient,
    memory_index: MemoryIndex,
    sid: str,
    turns: list[Turn],
    since_ts: str | None,
    primary_model_id: str,
    tools: dict[str, Tool],
    tool_config: dict[str, Any],
    system_block: list[dict[str, Any]],
    reason: str,
    tz_name: str | None = None,
) -> str | None:
    """Filter ``turns`` to those with ``ts > since_ts``, run a flush turn
    over them, then reindex_if_stale (which picks up freshly-flushed
    files). Returns the ts of the newest evaluated turn so the caller can
    advance its high-water mark, or ``None`` if nothing was flushed
    (either no growth or the flush turn raised).
    """
    new_turns = [t for t in turns if t.ts > (since_ts or "")]
    if not new_turns:
        log.debug("[%s] flush_then_reindex (reason=%s): nothing new to flush",
                  sid, reason)
        return None
    ok = await run_memory_flush(
        bedrock=bedrock,
        sid=sid,
        turns=new_turns,
        primary_model_id=primary_model_id,
        tools=tools,
        tool_config=tool_config,
        system_block=system_block,
        reason=reason,
        tz_name=tz_name,
    )
    if not ok:
        return None
    try:
        result = await memory_index.reindex_if_stale()
        log.debug("[%s] post-flush reindex: %s", sid, result)
    except Exception:
        log.exception("[%s] post-flush reindex failed", sid)
    return new_turns[-1].ts
