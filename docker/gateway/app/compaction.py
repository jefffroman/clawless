"""Transcript compaction — idle (eager, at boot) and mid-session (async).

Two distinct triggers, two distinct headings, both summarized by the
configured cheap model (Nova Micro by default):

* ``## Last Session Recap`` — runs eagerly during boot init when the prior
  session's last turn is older than IDLE_RECAP_SECONDS. Archives the old
  JSONL and prepends the recap as a system block on the new session.
  Synchronous (pre-live).
* ``## Pre-compaction Recap`` — runs mid-session when the estimated transcript
  token count exceeds the configured threshold. Runs as a background task
  off the user-reply critical path: summarize against a snapshot, then take
  the per-session lock and atomic-swap against the live transcript so any
  user turns that arrived during summarize are preserved.

If a transcript is still over ``hard_ceiling_tokens`` after the swap, the
caller continues with ``run_hard_reset`` to drop the transcript to just the
recap turn. Hard-reset does NOT re-flush — flush_then_reindex was already
run earlier in the same compaction cycle.

Summary blocks are stable across turns so they benefit from prompt caching.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from .bedrock import BedrockClient
from .channel import Channel
from .config import (
    IDLE_RECAP_SECONDS,
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


def will_mid_session_compact(cfg: Config, turns: list[Turn]) -> bool:
    """Predicate-only check (no I/O). The agent uses this to decide whether
    to spawn a background compaction task on the user-reply critical path.
    """
    return estimate_tokens(turns) > cfg.mid_session_token_threshold


async def run_mid_session_compact_async(
    *,
    cfg: Config,
    bedrock: BedrockClient,
    transcripts: TranscriptStore,
    channel: Channel,
    session_lock: asyncio.Lock,
    sid: str,
    peer_id: str,
    turns_snapshot: list[Turn],
) -> tuple[bool, list[Turn]]:
    """Run compaction off the user-reply critical path.

    Computes the older/newer split + summarize() against ``turns_snapshot``
    (which the caller captured at the moment compaction was decided to
    fire). The snapshot's older portion becomes a single recap turn;
    everything from the older boundary onward is preserved by re-reading
    the on-disk transcript at swap time — so any user turns that arrived
    during the summarize call survive.

    Returns ``(swapped, post_swap_turns)``. ``swapped=False`` means the
    predicate failed, the snapshot was too small to split, the summarize
    call failed, or another compactor raced and shrunk the live
    transcript past the snapshot's older boundary.

    Caller is responsible for any flush_then_reindex BEFORE invoking this;
    this function does not flush.
    """
    if not will_mid_session_compact(cfg, turns_snapshot):
        return False, turns_snapshot

    older, newer = _split_for_compaction(turns_snapshot)
    if not older:
        return False, turns_snapshot
    older_count = len(older)

    try:
        await channel.send(peer_id, MID_SESSION_NOTICE)
    except Exception:
        log.exception("could not send mid-session compaction notice; proceeding anyway")

    log.info(
        "[%s] background compaction: summarizing %d older turns (snapshot=%d)",
        sid, older_count, len(turns_snapshot),
    )

    text = _turns_to_text(older)
    try:
        summary = await bedrock.summarize(
            model_id=cfg.compaction_model_id,
            instruction=_MID_SESSION_INSTRUCTION,
            transcript_text=text,
        )
    except Exception:
        log.exception("[%s] background compaction summarize failed", sid)
        return False, turns_snapshot

    covers_from = older[0].ts
    covers_to = older[-1].ts
    compacted_at = _now().isoformat()

    block = (
        f"## Pre-compaction Recap\n"
        f"Covers turns from {covers_from} through {covers_to}.\n"
        f"Compacted at: {compacted_at}.\n\n"
        f"{summary}"
    )
    recap_turn = Turn(role="user", content=[{"text": block}], ts=compacted_at)

    # Atomic swap: under the session lock, re-read the live transcript (it
    # may have grown during summarize), and slice from older_count onward —
    # preserving the snapshot's newer portion AND any rows the agent
    # appended while we were summarizing.
    async with session_lock:
        current = transcripts.load(sid)
        if len(current) < older_count:
            log.warning(
                "[%s] background compaction skipped — transcript has %d turns "
                "but snapshot's older count was %d (raced?)",
                sid, len(current), older_count,
            )
            return False, current
        preserved = current[older_count:]
        new_turns = [recap_turn] + preserved
        transcripts.replace(sid, new_turns)

    log.info(
        "[%s] background compaction swapped: %d turns -> %d turns",
        sid, len(current), len(new_turns),
    )
    return True, new_turns


# ---------------------------------------------------------------------------
# Hard reset (post-compaction continuation, no flush)
# ---------------------------------------------------------------------------


async def run_hard_reset(
    *,
    cfg: Config,
    transcripts: TranscriptStore,
    session_lock: asyncio.Lock,
    sid: str,
    post_swap_turns: list[Turn],
) -> bool:
    """If the post-compaction transcript still exceeds ``hard_ceiling_tokens``,
    replace it with just the recap turn. The caller has already run
    flush_then_reindex earlier in the same compaction cycle; we do NOT
    flush again here.

    Returns True if a reset happened, False if no action was taken
    (under-ceiling or empty transcript).
    """
    size = estimate_tokens(post_swap_turns)
    if size <= cfg.hard_ceiling_tokens:
        return False
    log.warning(
        "[%s] post-compaction over hard ceiling (%d > %d); hard-reset to recap-only",
        sid, size, cfg.hard_ceiling_tokens,
    )
    async with session_lock:
        current = transcripts.load(sid)
        if not current:
            return False
        recap = current[0]
        # Preserve anything appended during the unlock window between the
        # compact-swap and this call (defensive; usually empty).
        appended_during_window = current[len(post_swap_turns):]
        new_turns = [recap] + appended_during_window
        transcripts.replace(sid, new_turns)
    log.info("[%s] hard-reset complete: transcript reduced to %d turn(s)",
             sid, len(new_turns))
    return True


__all__ = [
    "MID_SESSION_NOTICE",
    "maybe_idle_recap",
    "will_mid_session_compact",
    "run_mid_session_compact_async",
    "run_hard_reset",
]


# Avoid importer warnings if Any isn't otherwise used.
_ = Any
