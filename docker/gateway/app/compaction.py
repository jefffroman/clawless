"""Transcript compaction — idle (eager, at boot) and mid-session (synchronous).

Two distinct triggers, two distinct headings, both summarized by the
configured cheap model (Nova Micro by default):

* ``## Last Session Recap`` — runs eagerly during boot init when the prior
  session's last turn is older than IDLE_RECAP_SECONDS. Archives the old
  JSONL and prepends the recap as a system block on the new session.
* ``## Pre-compaction Recap`` — runs mid-session when the estimated transcript
  token count exceeds MID_SESSION_TOKEN_THRESHOLD. Sends a status notice via
  the channel first, then summarizes the oldest half, and replaces those
  turns with a single synthetic system block. The recent half is preserved.

Summary blocks are stable across turns so they benefit from prompt caching.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from .bedrock import BedrockClient
from .channel import Channel
from .config import (
    IDLE_RECAP_SECONDS,
    MID_SESSION_TOKEN_THRESHOLD,
    Config,
)
from .transcript import (
    TranscriptStore,
    Turn,
    estimate_tokens,
    parse_iso,
)

log = logging.getLogger("clawless.compaction")

MID_SESSION_NOTICE = (
    "One moment — compacting older context to keep things fast..."
)

_IDLE_INSTRUCTION = (
    "You are summarizing a prior conversation between a user and an AI agent. "
    "Produce a concise recap (≤200 words) that preserves: key decisions, "
    "outstanding questions, agreed-on facts, and anything the agent committed "
    "to do. Use bullet points. End with a one-line 'Open:' list of unresolved "
    "items, or 'Open: none.' if there are none."
)

_MID_SESSION_INSTRUCTION = (
    "You are summarizing the earlier portion of an in-progress conversation "
    "to free up context. Produce a concise recap (≤250 words) that preserves: "
    "key decisions, outstanding questions, agreed-on facts, tool results that "
    "remain relevant, and anything the agent committed to do. Use bullet "
    "points. End with a one-line 'Open:' list of unresolved items, or "
    "'Open: none.'"
)


def _human_delta(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"about {minutes} min ago"
    hours = minutes / 60
    if hours < 24:
        return f"about {hours:.1f} h ago"
    days = hours / 24
    return f"about {days:.1f} days ago"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _turns_to_text(turns: list[Turn]) -> str:
    """Render turns as a labeled transcript for the summarizer's eyes only."""
    lines: list[str] = []
    for t in turns:
        for block in t.content:
            if "text" in block and block["text"].strip():
                lines.append(f"[{t.role}] {block['text'].strip()}")
            elif "toolUse" in block:
                tu = block["toolUse"]
                lines.append(f"[{t.role} → tool {tu.get('name')}] input={tu.get('input')}")
            elif "toolResult" in block:
                inner = block["toolResult"].get("content", [])
                txt = " ".join(b.get("text", "") for b in inner if "text" in b)
                if txt:
                    lines.append(f"[tool result] {txt[:400]}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Idle recap (boot-time, eager)
# ---------------------------------------------------------------------------


async def maybe_idle_recap(
    *,
    cfg: Config,
    bedrock: BedrockClient,
    transcripts: TranscriptStore,
    sid: str,
) -> str | None:
    """If the most-recent turn is older than IDLE_RECAP_SECONDS, summarize the
    prior session, archive its JSONL, and return the system-block markdown
    for prepending to the next prompt. Otherwise return None.
    """
    last_ts = transcripts.last_ts(sid)
    if last_ts is None:
        return None
    last_unix = parse_iso(last_ts)
    age = time.time() - last_unix
    if age < IDLE_RECAP_SECONDS:
        return None

    turns = transcripts.load(sid)
    if not turns:
        return None

    log.info("idle recap: session %s last activity %s ago, summarizing %d turns",
             sid, _human_delta(age), len(turns))

    text = _turns_to_text(turns)
    try:
        summary = await bedrock.summarize(
            model_id=cfg.compaction_model_id,
            instruction=_IDLE_INSTRUCTION,
            transcript_text=text,
        )
    except Exception:
        log.exception("idle recap summarize failed; skipping recap")
        return None

    now_iso = _now().isoformat()
    archived = transcripts.archive(sid, f"recap-{now_iso.replace(':', '-')}")
    if archived:
        log.info("archived prior session to %s", archived)

    block = (
        f"## Last Session Recap\n"
        f"Last session ended {last_ts} ({_human_delta(age)}).\n"
        f"Current time: {now_iso}.\n\n"
        f"{summary}"
    )
    return block


# ---------------------------------------------------------------------------
# Mid-session compaction
# ---------------------------------------------------------------------------


def _split_for_compaction(turns: list[Turn]) -> tuple[list[Turn], list[Turn]]:
    """Split turns into (older_half, newer_half), respecting tool-pair atomicity.

    The model emits ``assistant → toolUse`` followed by a synthetic
    ``user → toolResult`` pair; both sides of the pair must travel together,
    or Bedrock will reject the next call with a tool-pairing error.

    We aim for the midpoint, then walk forward until we land on the boundary
    *after* a complete tool pair (or a plain user turn).
    """
    n = len(turns)
    if n < 4:
        return turns, []
    target = n // 2
    cut = target
    # Walk forward: the cut must be at a position where turns[cut].role == "user"
    # AND turns[cut].content is not a toolResult (otherwise we'd split a pair).
    while cut < n - 1:
        candidate = turns[cut]
        is_tool_result = candidate.role == "user" and any(
            "toolResult" in b for b in candidate.content
        )
        if not is_tool_result and candidate.role == "user":
            break
        cut += 1
    if cut >= n:
        return turns, []
    return turns[:cut], turns[cut:]


async def maybe_mid_session_compact(
    *,
    cfg: Config,
    bedrock: BedrockClient,
    transcripts: TranscriptStore,
    channel: Channel,
    sid: str,
    peer_id: str,
    turns: list[Turn],
) -> list[Turn]:
    """If ``turns`` exceeds the threshold, send a status notice, summarize
    the oldest half, replace those turns with a single recap turn, and return
    the new turns list. Otherwise return ``turns`` unchanged.
    """
    if estimate_tokens(turns) <= MID_SESSION_TOKEN_THRESHOLD:
        return turns

    older, newer = _split_for_compaction(turns)
    if not older:
        return turns

    try:
        await channel.send(peer_id, MID_SESSION_NOTICE)
    except Exception:
        log.exception("could not send mid-session compaction notice; proceeding anyway")

    log.info(
        "mid-session compaction: summarizing %d older turns, keeping %d recent",
        len(older), len(newer),
    )

    text = _turns_to_text(older)
    try:
        summary = await bedrock.summarize(
            model_id=cfg.compaction_model_id,
            instruction=_MID_SESSION_INSTRUCTION,
            transcript_text=text,
        )
    except Exception:
        log.exception("mid-session summarize failed; leaving transcript untouched")
        return turns

    covers_from = older[0].ts
    covers_to = older[-1].ts
    compacted_at = _now().isoformat()

    block = (
        f"## Pre-compaction Recap\n"
        f"Covers turns from {covers_from} through {covers_to}.\n"
        f"Compacted at: {compacted_at}.\n\n"
        f"{summary}"
    )

    # Synthesize a single user turn carrying the recap as text. This shape
    # replays cleanly into messages[] without needing a special role.
    recap_turn = Turn(
        role="user",
        content=[{"text": block}],
        ts=compacted_at,
    )
    new_turns = [recap_turn] + newer
    transcripts.replace(sid, new_turns)
    return new_turns


__all__ = [
    "MID_SESSION_NOTICE",
    "maybe_idle_recap",
    "maybe_mid_session_compact",
]


# Avoid importer warnings if Any isn't otherwise used.
_ = Any
