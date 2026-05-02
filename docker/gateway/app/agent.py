"""Agent orchestrator — wires channel inbound to bedrock + tools + memory.

One ``Agent`` instance per gateway process; per-peer state (sessions) lives
in the transcript store keyed by ``session_id``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from .bedrock import BedrockClient
from .channel import Channel, InboundMessage
from .compaction import maybe_idle_recap, maybe_mid_session_compact
from .config import Config
from .memory import MemoryIndex
from .tools import Tool, bedrock_tool_config
from .transcript import TranscriptStore, session_id

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

    def _load_system_template(self) -> str:
        path = os.path.join(os.path.dirname(__file__), "system_prompt.md")
        try:
            with open(path) as f:
                return f.read()
        except OSError:
            return "You are a helpful AI agent."

    def _system_for(self, recap_block: str | None, retrieval_block: str | None) -> list[dict[str, Any]]:
        rendered = self.system_template.replace("{AGENT_NAME}", self.cfg.agent_name)
        rendered = rendered.replace("${WORKSPACE_DIR}", self.cfg.workspace_dir)
        parts = [rendered]
        if recap_block:
            parts.append(recap_block)
        if retrieval_block:
            parts.append(retrieval_block)
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
        turns = await maybe_mid_session_compact(
            cfg=self.cfg,
            bedrock=self.bedrock,
            transcripts=self.transcripts,
            channel=self.channel,
            sid=sid,
            peer_id=msg.peer_id,
            turns=turns,
        )

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
