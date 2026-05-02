"""Environment-driven config for clawless-gateway.

Single source of truth for env-var consumers. Strips the LiteLLM-style
`bedrock/` prefix from model IDs at load (Bedrock's converse_stream wants the
bare model ID).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

# Bedrock's converse_stream expects bare model IDs like
# "us.anthropic.claude-haiku-4-5-20251001-v1:0"; the LiteLLM-style "bedrock/"
# prefix is OpenClaw lineage we strip here once.
_BEDROCK_PREFIX = "bedrock/"

COMPACTION_MODEL_DEFAULT = "us.amazon.nova-micro-v1:0"

# Estimated token threshold above which mid-session compaction kicks in.
# Naive len/4 estimator; we err on the conservative side and let prompt-cache
# soak up the rebuilt prefix.
MID_SESSION_TOKEN_THRESHOLD = 24_000

# Idle threshold for wake-time recap: anything older than this gets summarized
# into a "Last Session Recap" block prepended to the new session's prompt.
IDLE_RECAP_SECONDS = 3600

# Stale-claim window for wake-greet DDB rows. If a prior boot crashed between
# claim and delete, the next boot can re-claim after this many seconds.
WAKE_CLAIM_STALE_SECONDS = 600

# Bound for tool-use loops in a single turn. Bedrock can chain toolUse →
# toolResult indefinitely; cap to keep runaway loops from burning tokens.
MAX_TOOL_TURNS = 10

# Telegram message size cap. Hard limit is 4096; we soft-cap below to leave
# headroom for UTF-8 multibyte glyphs and quoted-reply formatting.
TELEGRAM_CHUNK_MAX = 3500

HEALTH_HOST = "127.0.0.1"
HEALTH_PORT = 18789


def _strip_bedrock_prefix(model_id: str) -> str:
    if model_id.startswith(_BEDROCK_PREFIX):
        return model_id[len(_BEDROCK_PREFIX):]
    return model_id


@dataclass(frozen=True)
class Config:
    agent_slug: str
    agent_name: str
    slug_safe: str

    backup_bucket: str
    aws_region: str
    ecs_cluster: str

    model_id: str
    channel: str
    channel_config: dict[str, Any]

    lifecycle_sfn_arn: str
    wake_listener_url: str
    wake_messages_table: str
    searxng_url: str

    workspace_dir: str
    memory_data_dir: str

    verbose: bool

    compaction_model_id: str = COMPACTION_MODEL_DEFAULT

    @property
    def memory_source_dir(self) -> str:
        # New layout: workspace files live directly under WORKSPACE_DIR/memory/
        # rather than under .openclaw/workspace/.
        return os.path.join(self.workspace_dir, "memory")

    @property
    def transcripts_dir(self) -> str:
        return os.path.join(self.workspace_dir, "transcripts")


def _require(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise SystemExit(f"missing required env var: {name}")
    return val


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def load() -> Config:
    agent_slug = _require("AGENT_SLUG")
    slug_safe = re.sub(r"[^A-Za-z0-9_-]", "-", agent_slug)

    raw_channel_config = os.environ.get("CLAWLESS_CHANNEL_CONFIG", "").strip()
    channel_config: dict[str, Any] = {}
    if raw_channel_config:
        try:
            channel_config = json.loads(raw_channel_config)
        except json.JSONDecodeError as e:
            raise SystemExit(f"CLAWLESS_CHANNEL_CONFIG is not valid JSON: {e}") from e

    return Config(
        agent_slug=agent_slug,
        agent_name=os.environ.get("AGENT_NAME", "").strip() or slug_safe,
        slug_safe=slug_safe,
        backup_bucket=_require("BACKUP_BUCKET"),
        aws_region=os.environ.get("AWS_DEFAULT_REGION", "us-east-1").strip(),
        ecs_cluster=os.environ.get("ECS_CLUSTER", "").strip(),
        model_id=_strip_bedrock_prefix(_require("CLAWLESS_MODEL")),
        channel=_require("CLAWLESS_CHANNEL").lower(),
        channel_config=channel_config,
        lifecycle_sfn_arn=os.environ.get("LIFECYCLE_SFN_ARN", "").strip(),
        wake_listener_url=os.environ.get("WAKE_LISTENER_URL", "").strip(),
        wake_messages_table=os.environ.get("WAKE_MESSAGES_TABLE", "").strip(),
        searxng_url=os.environ.get("SEARXNG_URL", "").strip(),
        workspace_dir=os.environ.get("WORKSPACE_DIR", "/home/clawless").rstrip("/"),
        memory_data_dir=os.environ.get("MEMORY_DATA_DIR", "/var/lib/clawless-memory"),
        verbose=_bool("CLAWLESS_VERBOSE"),
        compaction_model_id=_strip_bedrock_prefix(
            os.environ.get("CLAWLESS_COMPACTION_MODEL", "").strip()
            or COMPACTION_MODEL_DEFAULT
        ),
    )
