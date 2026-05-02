"""Bedrock Converse streaming wrapper + tool-use loop.

boto3 is sync; we drive ``client.converse_stream`` through
``loop.run_in_executor`` and a bounded asyncio.Queue so the asyncio event loop
stays responsive while the streaming response unspools in a worker thread.

The tool-use loop runs at most ``MAX_TOOL_TURNS`` cycles per inbound message:
on each cycle, if the model emitted any toolUse content blocks, we execute
them, append a user turn carrying the toolResult blocks, and call converse
again. Otherwise we return the accumulated text.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import threading
from typing import Any

import boto3
from botocore.config import Config as BotoConfig

from .config import MAX_TOOL_TURNS, Config
from .tools import Tool

log = logging.getLogger("clawless.bedrock")

# Bound the streaming queue so a slow consumer doesn't let the producer thread
# accumulate unbounded events in memory. 64 is well above any realistic
# streaming chunk count for a single Bedrock response.
_STREAM_QUEUE_MAX = 64

_END_OF_STREAM = object()


def _make_client(region: str) -> Any:
    cfg = BotoConfig(retries={"max_attempts": 3, "mode": "adaptive"})
    return boto3.client("bedrock-runtime", region_name=region, config=cfg)


class BedrockClient:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.client = _make_client(cfg.aws_region)
        # Separate client used for cheap-model summarization (compaction).
        self.compaction_client = self.client

    async def converse_once(
        self,
        *,
        model_id: str,
        messages: list[dict[str, Any]],
        system: list[dict[str, Any]] | None = None,
        tool_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """One Bedrock converse_stream call. Returns the final assistant turn
        plus a stop reason. Streams events through a queue but assembles them
        into a single message before returning."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=_STREAM_QUEUE_MAX)

        kwargs: dict[str, Any] = {"modelId": model_id, "messages": messages}
        if system:
            kwargs["system"] = system
        if tool_config:
            kwargs["toolConfig"] = tool_config

        def _producer() -> None:
            try:
                resp = self.client.converse_stream(**kwargs)
                for event in resp["stream"]:
                    asyncio.run_coroutine_threadsafe(queue.put(event), loop).result()
            except Exception as e:
                asyncio.run_coroutine_threadsafe(queue.put(("__error__", e)), loop).result()
            finally:
                asyncio.run_coroutine_threadsafe(queue.put(_END_OF_STREAM), loop).result()

        threading.Thread(target=_producer, daemon=True, name="bedrock-stream").start()

        # Accumulator state
        content_blocks: list[dict[str, Any]] = []
        block_starts: dict[int, dict[str, Any]] = {}
        tool_input_buffers: dict[int, str] = {}
        stop_reason: str | None = None
        usage: dict[str, Any] | None = None

        while True:
            event = await queue.get()
            if event is _END_OF_STREAM:
                break
            if isinstance(event, tuple) and event and event[0] == "__error__":
                raise event[1]
            self._absorb_event(event, content_blocks, block_starts, tool_input_buffers)
            if "messageStop" in event:
                stop_reason = event["messageStop"].get("stopReason")
            if "metadata" in event:
                usage = event["metadata"].get("usage")

        return {
            "role": "assistant",
            "content": content_blocks,
            "stop_reason": stop_reason,
            "usage": usage,
        }

    @staticmethod
    def _absorb_event(
        event: dict[str, Any],
        content_blocks: list[dict[str, Any]],
        block_starts: dict[int, dict[str, Any]],
        tool_input_buffers: dict[int, str],
    ) -> None:
        if "contentBlockStart" in event:
            cbs = event["contentBlockStart"]
            idx = cbs["contentBlockIndex"]
            start = cbs.get("start", {})
            block_starts[idx] = start
            if "toolUse" in start:
                tu = start["toolUse"]
                content_blocks.append({
                    "toolUse": {
                        "toolUseId": tu["toolUseId"],
                        "name": tu["name"],
                        "input": {},
                    }
                })
                tool_input_buffers[idx] = ""
            else:
                content_blocks.append({"text": ""})
        elif "contentBlockDelta" in event:
            cbd = event["contentBlockDelta"]
            idx = cbd["contentBlockIndex"]
            delta = cbd.get("delta", {})
            if idx >= len(content_blocks):
                # contentBlockStart was skipped (text-only response): seed.
                content_blocks.append({"text": ""})
            block = content_blocks[idx]
            if "text" in delta:
                if "text" in block:
                    block["text"] += delta["text"]
                else:
                    block["text"] = delta["text"]
            elif "toolUse" in delta:
                tu_delta = delta["toolUse"]
                if "input" in tu_delta:
                    tool_input_buffers[idx] = tool_input_buffers.get(idx, "") + tu_delta["input"]
        elif "contentBlockStop" in event:
            idx = event["contentBlockStop"]["contentBlockIndex"]
            if idx in tool_input_buffers:
                raw = tool_input_buffers.pop(idx)
                try:
                    parsed = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    parsed = {"_raw": raw}
                if idx < len(content_blocks) and "toolUse" in content_blocks[idx]:
                    content_blocks[idx]["toolUse"]["input"] = parsed

    # -----------------------------------------------------------------------
    # Tool-use loop
    # -----------------------------------------------------------------------

    async def run_turn(
        self,
        *,
        model_id: str,
        history: list[dict[str, Any]],
        system: list[dict[str, Any]],
        tools: dict[str, Tool],
        tool_config: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], str]:
        """Drive converse calls until the model stops requesting tools.

        Returns ``(new_turns, final_text)`` where ``new_turns`` is the list of
        Bedrock-shaped messages to append to the transcript (assistant turns
        with toolUse, user turns with toolResult, and the final assistant
        text), and ``final_text`` is the concatenated text of the last
        assistant turn (for sending to the user).
        """
        new_turns: list[dict[str, Any]] = []
        messages = list(history)

        for turn_idx in range(MAX_TOOL_TURNS):
            response = await self.converse_once(
                model_id=model_id,
                messages=messages,
                system=system,
                tool_config=tool_config,
            )
            assistant_turn = {"role": "assistant", "content": response["content"]}
            new_turns.append(assistant_turn)
            messages.append(assistant_turn)

            tool_uses = [b["toolUse"] for b in response["content"] if "toolUse" in b]
            if response.get("stop_reason") != "tool_use" or not tool_uses:
                final_text = "".join(b.get("text", "") for b in response["content"] if "text" in b)
                return new_turns, final_text

            log.info("turn %d: %d tool_use(s) requested", turn_idx, len(tool_uses))
            tool_result_blocks = await self._run_tools(tool_uses, tools)
            user_turn = {"role": "user", "content": tool_result_blocks}
            new_turns.append(user_turn)
            messages.append(user_turn)

        log.warning("hit MAX_TOOL_TURNS=%d without final assistant text", MAX_TOOL_TURNS)
        return new_turns, "[hit tool-use limit; please try again]"

    @staticmethod
    async def _run_tools(
        tool_uses: list[dict[str, Any]],
        tools: dict[str, Tool],
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for tu in tool_uses:
            name = tu["name"]
            tool = tools.get(name)
            if tool is None:
                results.append({
                    "toolResult": {
                        "toolUseId": tu["toolUseId"],
                        "content": [{"text": f"error: unknown tool {name!r}"}],
                        "status": "error",
                    }
                })
                continue
            try:
                output = await tool.run(tu.get("input", {}))
                status = "success"
            except Exception as e:
                log.exception("tool %s failed", name)
                output = f"error: {e}"
                status = "error"
            results.append({
                "toolResult": {
                    "toolUseId": tu["toolUseId"],
                    "content": [{"text": output or "(empty)"}],
                    "status": status,
                }
            })
        return results

    # -----------------------------------------------------------------------
    # Compaction summarizer (cheap-model one-shot, no tools)
    # -----------------------------------------------------------------------

    async def summarize(
        self,
        *,
        model_id: str,
        instruction: str,
        transcript_text: str,
    ) -> str:
        """Run a non-streaming converse for a one-shot summary."""
        loop = asyncio.get_running_loop()

        def _go() -> dict[str, Any]:
            return self.compaction_client.converse(
                modelId=model_id,
                messages=[{
                    "role": "user",
                    "content": [{"text": f"{instruction}\n\n--- transcript ---\n{transcript_text}"}],
                }],
            )

        try:
            response = await loop.run_in_executor(None, _go)
        except Exception:
            log.exception("summarize call failed")
            raise

        out = response.get("output", {}).get("message", {}).get("content", [])
        return "".join(b.get("text", "") for b in out if "text" in b).strip()
