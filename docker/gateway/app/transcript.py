"""JSONL append-only transcript store keyed by session ID.

Schema matches Bedrock Converse content blocks verbatim so transcripts replay
into `messages[]` without translation:

    {"role": "user"|"assistant", "content": [<content blocks>], "ts": "<ISO>"}

Content blocks follow Bedrock's shape:
- {"text": "..."}
- {"toolUse": {"toolUseId": "...", "name": "...", "input": {...}}}
- {"toolResult": {"toolUseId": "...", "content": [{"text": "..."}], "status": "success"|"error"}}
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9_-]")


def session_id(channel: str, peer_id: str | int) -> str:
    """Return a filename-safe session id for ``channel`` and ``peer_id``.

    Telegram peer IDs can be negative (group chats) and would break path joins;
    sanitize aggressively.
    """
    return _FILENAME_SAFE.sub("_", f"{channel}_{peer_id}")


def _path(transcripts_dir: str, sid: str) -> str:
    return os.path.join(transcripts_dir, f"{sid}.jsonl")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Turn:
    role: str
    content: list[dict[str, Any]]
    ts: str

    def as_message(self) -> dict[str, Any]:
        """Bedrock-shaped message (role + content), no ts."""
        return {"role": self.role, "content": self.content}


class TranscriptStore:
    def __init__(self, transcripts_dir: str) -> None:
        self.dir = transcripts_dir
        os.makedirs(self.dir, exist_ok=True)

    def load(self, sid: str) -> list[Turn]:
        path = _path(self.dir, sid)
        if not os.path.exists(path):
            return []
        turns: list[Turn] = []
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                turns.append(Turn(
                    role=obj.get("role", "user"),
                    content=obj.get("content", []),
                    ts=obj.get("ts", _now_iso()),
                ))
        return turns

    def append(self, sid: str, role: str, content: list[dict[str, Any]], ts: str | None = None) -> Turn:
        turn = Turn(role=role, content=content, ts=ts or _now_iso())
        path = _path(self.dir, sid)
        with open(path, "a") as f:
            f.write(json.dumps({"role": turn.role, "content": turn.content, "ts": turn.ts}) + "\n")
        return turn

    def replace(self, sid: str, turns: list[Turn]) -> None:
        """Rewrite the transcript atomically (used by mid-session compaction)."""
        path = _path(self.dir, sid)
        tmp = f"{path}.tmp"
        with open(tmp, "w") as f:
            for t in turns:
                f.write(json.dumps({"role": t.role, "content": t.content, "ts": t.ts}) + "\n")
        os.replace(tmp, path)

    def archive(self, sid: str, suffix: str) -> str | None:
        """Move the transcript aside so a fresh one can start. Returns archived path."""
        path = _path(self.dir, sid)
        if not os.path.exists(path):
            return None
        archived = os.path.join(self.dir, f"{sid}.{suffix}.jsonl")
        os.replace(path, archived)
        return archived

    def last_ts(self, sid: str) -> str | None:
        """Return the ts of the final turn, or None if no transcript exists.

        Reads only the tail of the file rather than the whole transcript.
        """
        path = _path(self.dir, sid)
        if not os.path.exists(path):
            return None
        try:
            size = os.path.getsize(path)
            if size == 0:
                return None
            with open(path, "rb") as f:
                # Read last 4 KB; final JSONL line is virtually always shorter.
                read_from = max(0, size - 4096)
                f.seek(read_from)
                tail = f.read().decode("utf-8", errors="replace")
            for line in reversed(tail.splitlines()):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    return obj.get("ts")
                except json.JSONDecodeError:
                    continue
        except OSError:
            return None
        return None


def estimate_tokens(turns: list[Turn]) -> int:
    """Naive ~chars/4 estimator; good enough for a compaction trigger."""
    chars = 0
    for t in turns:
        for block in t.content:
            if "text" in block:
                chars += len(block.get("text", ""))
            elif "toolUse" in block:
                tu = block["toolUse"]
                chars += len(json.dumps(tu.get("input", {})))
                chars += len(tu.get("name", ""))
            elif "toolResult" in block:
                for inner in block["toolResult"].get("content", []):
                    if "text" in inner:
                        chars += len(inner["text"])
    return chars // 4


def parse_iso(ts: str) -> float:
    """Return a unix timestamp from an ISO-8601 string."""
    try:
        return datetime.fromisoformat(ts).timestamp()
    except (TypeError, ValueError):
        return time.time()
