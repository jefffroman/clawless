"""Agent orchestrator — wires channel inbound to bedrock + tools + memory.

One ``Agent`` instance per gateway process; per-peer state (sessions) lives
in the transcript store keyed by ``session_id``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from .bedrock import BedrockClient
from .channel import Channel, InboundMessage
from .compaction import (
    maybe_idle_recap,
    run_hard_reset,
    run_mid_session_compact_async,
    will_mid_session_compact,
)
from .config import Config
from .memory import ROOT_SOURCES, MemoryIndex
from .memory_flush import flush_then_reindex
from .tools import Tool, bedrock_tool_config
from .transcript import TranscriptStore, Turn, estimate_tokens, session_id

log = logging.getLogger("clawless.agent")


def _is_archived_transcript(filename: str) -> bool:
    """Archived transcripts have suffixes like ``.recap-<ts>.jsonl`` or
    ``.reset.<ts>.jsonl``. Skip them when enumerating live sessions."""
    return ".recap-" in filename or ".reset" in filename


class Agent:
    def __init__(
        self,
        cfg: Config,
        bedrock: BedrockClient,
        memory: MemoryIndex,
        tools: dict[str, Tool],
        transcripts: TranscriptStore,
        channel: Channel,
    ) -> None:
        self.cfg = cfg
        self.bedrock = bedrock
        self.memory = memory
        self.tools = tools
        self.tool_config = bedrock_tool_config(tools)
        self.transcripts = transcripts
        self.channel = channel
        self.system_template = self._load_system_template()
        self._idle_recapped: set[str] = set()
        self._idle_recap_blocks: dict[str, str] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        # Sessions with a background compaction task in flight; prevents
        # spawning a second compactor while one is still running.
        self._bg_compaction_sids: set[str] = set()
        # Sessions with a flush_then_reindex in flight; a triggered flush
        # for an sid already in this set is skipped (logged but a no-op),
        # not queued. asyncio is single-threaded so check-and-add is atomic
        # as long as no `await` interleaves between them.
        self._flushing_sids: set[str] = set()
        # Strong refs to background tasks — asyncio holds only weak refs to
        # tasks created via create_task, so unreferenced tasks may be GC'd
        # mid-execution. Discard each task on completion.
        self._bg_tasks: set[asyncio.Task[None]] = set()
        # Per-session ISO ts of the newest turn included in the most recent
        # successful flush. Persisted across restarts in flush_state.json so
        # incremental flush survives sleep/wake.
        self._flush_state_path = os.path.join(
            cfg.memory_source_dir, ".flush_state.json"
        )
        self._last_flush_ts: dict[str, str] = self._load_flush_state()

    def _load_system_template(self) -> str:
        path = os.path.join(os.path.dirname(__file__), "system_prompt.md")
        try:
            with open(path) as f:
                return f.read()
        except OSError:
            return "You are a helpful AI agent."

    # --- flush-state persistence -------------------------------------------

    def _load_flush_state(self) -> dict[str, str]:
        try:
            with open(self._flush_state_path) as f:
                obj = json.load(f)
            if isinstance(obj, dict):
                return {str(k): str(v) for k, v in obj.items()}
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        except Exception:
            log.exception("failed to load flush state; starting fresh")
        return {}

    def _save_flush_state(self) -> None:
        os.makedirs(os.path.dirname(self._flush_state_path), exist_ok=True)
        tmp = f"{self._flush_state_path}.tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(self._last_flush_ts, f, indent=2)
            os.replace(tmp, self._flush_state_path)
        except OSError:
            log.exception("failed to persist flush state")

    def mark_flushed(self, sid: str, last_ts: str) -> None:
        """Advance the per-session high-water mark and persist."""
        if not last_ts:
            return
        self._last_flush_ts[sid] = last_ts
        self._save_flush_state()

    def session_growth(self, sid: str) -> int:
        """Estimate tokens of turns whose ts > last-flush mark."""
        since = self._last_flush_ts.get(sid, "")
        new_turns = [t for t in self.transcripts.load(sid) if t.ts > since]
        return estimate_tokens(new_turns)

    def known_session_ids(self) -> list[str]:
        """All session IDs with on-disk transcripts (live, not archived)."""
        if not os.path.isdir(self.cfg.transcripts_dir):
            return []
        sids: list[str] = []
        for entry in os.listdir(self.cfg.transcripts_dir):
            if not entry.endswith(".jsonl"):
                continue
            if _is_archived_transcript(entry):
                continue
            sids.append(entry[:-len(".jsonl")])
        return sids

    def init_flush_state_for_existing(self) -> None:
        """Initialize ``_last_flush_ts`` for any session present on disk
        with no entry yet — set to the last turn's ts so historical content
        is treated as already-flushed (avoids a one-time massive flush of
        pre-existing transcripts on first deployment of incremental flush).
        """
        changed = False
        for sid in self.known_session_ids():
            if sid in self._last_flush_ts:
                continue
            last_ts = self.transcripts.last_ts(sid)
            if last_ts:
                self._last_flush_ts[sid] = last_ts
                changed = True
        if changed:
            self._save_flush_state()

    def _system_for(self, recap_block: str | None, retrieval_block: str | None) -> list[dict[str, Any]]:
        rendered = self.system_template.replace("{AGENT_NAME}", self.cfg.agent_name)
        rendered = rendered.replace("${WORKSPACE_DIR}", self.cfg.workspace_dir)
        parts = [rendered]
        if recap_block:
            parts.append(recap_block)
        if retrieval_block:
            parts.append(retrieval_block)
        return [{"text": "\n\n".join(parts)}]

    def _build_flush_system_block(self) -> list[dict[str, Any]]:
        """Flush-specific system block: rendered system template PLUS a
        snapshot of top-level memory files (MEMORY.md, USER.md, AGENTS.md,
        etc.). The flush agent uses these to decide what's already
        captured vs. what's genuinely new in the conversation excerpt.

        Daily notes (memory/YYYY-MM-DD.md) are intentionally excluded —
        those are exactly what flush is producing, so feeding them back
        in would be circular and grow unbounded.
        """
        rendered = self.system_template.replace("{AGENT_NAME}", self.cfg.agent_name)
        rendered = rendered.replace("${WORKSPACE_DIR}", self.cfg.workspace_dir)
        parts = [rendered]

        memory_sections: list[str] = []
        for fname in ROOT_SOURCES:
            fpath = os.path.join(self.cfg.memory_source_dir, fname)
            try:
                with open(fpath) as f:
                    content = f.read()
            except (FileNotFoundError, OSError):
                continue
            if not content.strip():
                continue
            memory_sections.append(f"### memory/{fname}\n{content.rstrip()}")

        if memory_sections:
            parts.append(
                "## Memory files (current state)\n\n"
                "Snapshot of your top-level memory files. Cross-reference "
                "against these when deciding what durable knowledge from the "
                "conversation excerpt is worth appending — skip what's "
                "already captured here.\n\n"
                + "\n\n".join(memory_sections)
            )

        return [{"text": "\n\n".join(parts)}]

    async def boot_recap_known_sessions(self) -> None:
        """Eagerly run idle compaction on every existing transcript before
        going live. Adds 1-2 s per stale session to cold-wake but means the
        first user turn after wake doesn't pay the latency."""
        if not os.path.isdir(self.cfg.transcripts_dir):
            return
        for entry in sorted(os.listdir(self.cfg.transcripts_dir)):
            if not entry.endswith(".jsonl"):
                continue
            if _is_archived_transcript(entry):
                continue
            sid = entry[:-len(".jsonl")]
            try:
                block = await maybe_idle_recap(
                    cfg=self.cfg,
                    bedrock=self.bedrock,
                    transcripts=self.transcripts,
                    sid=sid,
                )
            except Exception:
                log.exception("boot recap failed for %s", sid)
                self._idle_recapped.add(sid)
                continue
            self._idle_recapped.add(sid)
            if block:
                self._idle_recap_blocks[sid] = block
                log.info("boot recap installed for session %s", sid)

    async def handle_inbound(self, msg: InboundMessage) -> None:
        """Single inbound message → one full Bedrock turn (with tool loop) →
        one (chunked) outbound reply.

        The whole turn runs inside the channel's typing-indicator context so
        the user sees a continuous "typing…" signal across memory retrieval,
        any mid-session compaction, the Bedrock stream, and tool calls.
        """
        sid = session_id(msg.channel, msg.peer_id)
        lock = self._session_locks.setdefault(sid, asyncio.Lock())
        async with lock:
            async with self.channel.typing(msg.peer_id):
                await self._process(sid, msg)

    async def _process(self, sid: str, msg: InboundMessage) -> None:
        recap_block: str | None = None
        if sid not in self._idle_recapped:
            try:
                recap_block = await maybe_idle_recap(
                    cfg=self.cfg,
                    bedrock=self.bedrock,
                    transcripts=self.transcripts,
                    sid=sid,
                )
            except Exception:
                log.exception("on-demand idle recap failed for %s", sid)
            self._idle_recapped.add(sid)
            if recap_block:
                self._idle_recap_blocks[sid] = recap_block
        else:
            recap_block = self._idle_recap_blocks.get(sid)

        try:
            retrieval_block = await self.memory.retrieve_markdown(msg.text, top_n=5, compact=True)
        except Exception:
            log.exception("memory retrieve failed; continuing without context")
            retrieval_block = None

        user_text = f"{msg.sender_name}: {msg.text}" if msg.sender_name else msg.text
        self.transcripts.append(sid, "user", [{"text": user_text}])

        turns = self.transcripts.load(sid)

        # Spawn a background compaction task if the threshold is tripped and
        # one isn't already running. The user reply path proceeds against
        # ``turns`` (full uncompacted history); the bg task will rewrite the
        # on-disk transcript after the user reply finishes (it acquires the
        # session lock for the swap). A flush_then_reindex precedes the
        # compaction inside the bg task.
        if (sid not in self._bg_compaction_sids
                and will_mid_session_compact(self.cfg, turns)):
            self._bg_compaction_sids.add(sid)
            task = asyncio.create_task(
                self._run_bg_compaction(sid, list(turns), msg.peer_id),
                name=f"compact-{sid}",
            )
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)

        history = [t.as_message() for t in turns]
        system = self._system_for(recap_block, retrieval_block)

        try:
            new_turns, final_text = await self.bedrock.run_turn(
                model_id=self.cfg.model_id,
                history=history,
                system=system,
                tools=self.tools,
                tool_config=self.tool_config,
            )
        except Exception:
            log.exception("bedrock run_turn failed")
            await self.channel.send(msg.peer_id, "Sorry — I hit an error. Could you try again?")
            return

        for t in new_turns:
            self.transcripts.append(sid, t["role"], t["content"])

        if final_text.strip():
            await self.channel.send(msg.peer_id, final_text.strip())

    async def flush_session(self, sid: str, reason: str) -> None:
        """Run flush_then_reindex for one session under the per-session
        flush lock. Skips silently (logged) if another flush is already
        in flight for the same sid — flushes are not queued, just
        deduplicated.

        Caller-supplied ``reason`` becomes the log label
        ("pre-sleep" / "pre-compact" / "periodic-growth").
        """
        # Check-and-add must not be interleaved with await between the two
        # operations; asyncio is single-threaded so this is atomic.
        if sid in self._flushing_sids:
            log.info(
                "[%s] flush skipped (reason=%s): another flush in flight",
                sid, reason,
            )
            return
        self._flushing_sids.add(sid)
        try:
            turns = self.transcripts.load(sid)
            latest_ts = await flush_then_reindex(
                bedrock=self.bedrock,
                memory_index=self.memory,
                sid=sid,
                turns=turns,
                since_ts=self._last_flush_ts.get(sid),
                primary_model_id=self.cfg.model_id,
                tools=self.tools,
                tool_config=self.tool_config,
                system_block=self._build_flush_system_block(),
                reason=reason,
                tz_name=None,
            )
            if latest_ts:
                self.mark_flushed(sid, latest_ts)
        except Exception:
            log.exception("[%s] flush_then_reindex failed (reason=%s)", sid, reason)
        finally:
            self._flushing_sids.discard(sid)

    async def flush_all_sessions_before_sleep(self) -> None:
        """Run flush_session for every known session before SIGTERM.

        Called by the sleep-tool wrapper so durable knowledge is captured
        regardless of whether the agent chose to write anything itself
        during the sleep dialogue.
        """
        for sid in self.known_session_ids():
            await self.flush_session(sid, reason="pre-sleep")

    async def _run_bg_compaction(
        self, sid: str, snapshot: list[Turn], peer_id: str,
    ) -> None:
        """Background compaction task: flush_then_reindex (incremental
        window over the snapshot), then mid-session compaction with
        atomic-swap, then hard-reset if still over the hard ceiling.

        Runs concurrently with the user-reply turn for the inbound message
        that tripped the threshold. The summarize and flush calls happen
        without holding the session lock; the swap takes the lock briefly.
        """
        lock = self._session_locks.setdefault(sid, asyncio.Lock())
        try:
            await self.flush_session(sid, reason="pre-compact")

            swapped, post_turns = await run_mid_session_compact_async(
                cfg=self.cfg,
                bedrock=self.bedrock,
                transcripts=self.transcripts,
                channel=self.channel,
                session_lock=lock,
                sid=sid,
                peer_id=peer_id,
                turns_snapshot=snapshot,
            )
            if swapped:
                await run_hard_reset(
                    cfg=self.cfg,
                    transcripts=self.transcripts,
                    session_lock=lock,
                    sid=sid,
                    post_swap_turns=post_turns,
                )
        except Exception:
            log.exception("[%s] background compaction task failed", sid)
        finally:
            self._bg_compaction_sids.discard(sid)
