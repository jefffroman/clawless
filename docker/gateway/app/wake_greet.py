"""Wake-greet: claim-deliver-delete protocol against the wake_messages DDB.

The wake_listener Lambda queues inbound Telegram messages to a DynamoDB row
keyed by ``slug`` while the gateway is asleep. On boot, we:

1. **Claim** the row atomically with a conditional UpdateItem
   (``attribute_not_exists(claimed_at) OR claimed_at < :stale``). If the
   condition fails, another task already owns it (or the row no longer exists);
   skip.
2. **Replay** queued messages by feeding them into the same ``handle_inbound``
   path that ordinary channel events take.
3. **Delete** the row once the agent's reply has been telegram-sent. Conditional
   on our own claim timestamp so we can't delete a row a future task re-claimed
   after we expired.

PII (user message text) lives in DDB only between ``claimed_at`` and the
delete: usually a few seconds. The wake_listener-side TTL (7 days) is a backstop
only.

On crash between claim and delete: ``claimed_at`` lingers; the next boot
re-claims after the stale window and re-replays. Duplicate delivery is the
accepted at-least-once tradeoff.
"""

from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal
from typing import Any, Awaitable, Callable

import boto3
from botocore.exceptions import ClientError

from .channel import InboundMessage
from .config import WAKE_CLAIM_STALE_SECONDS, Config

log = logging.getLogger("clawless.wake")

InboundHandler = Callable[[InboundMessage], Awaitable[None]]


def _decimal_to_native(obj: Any) -> Any:
    if isinstance(obj, list):
        return [_decimal_to_native(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _decimal_to_native(v) for k, v in obj.items()}
    if isinstance(obj, Decimal):
        return int(obj) if obj == int(obj) else float(obj)
    return obj


async def wake_greet(
    *,
    cfg: Config,
    on_message: InboundHandler,
) -> int:
    """Replay queued wake messages, or fire a synthetic 'Hello <agent_name>'.

    The default greeting matches OpenClaw's wake behaviour: it surfaces an
    agent reply on every wake even when no inbound is queued (e.g. when the
    operator wakes via wake-agent.sh). The synthetic message is fed through
    the same handler the channel uses, so the agent's reply lands in the
    user's chat the same way as a real turn.

    Returns the number of messages dispatched (queued + synthetic).
    """
    replayed = await replay_queued_messages(cfg=cfg, on_message=on_message)
    if replayed > 0:
        return replayed
    return await _send_default_greeting(cfg=cfg, on_message=on_message)


async def _send_default_greeting(
    *,
    cfg: Config,
    on_message: InboundHandler,
) -> int:
    peer = _peer_from_config(cfg)
    if not peer:
        log.info("no allowlist peer; skipping default greeting")
        return 0
    text = f"Hello, {cfg.agent_name}."
    log.info("no wake queue; firing default greeting to %s", peer)
    try:
        await on_message(InboundMessage(
            peer_id=peer,
            sender_name="",  # synthetic — no real sender, suppresses the "Name:" prefix
            text=text,
            channel=cfg.channel,
        ))
    except Exception:
        log.exception("default greeting handler failed")
        return 0
    return 1


async def replay_queued_messages(
    *,
    cfg: Config,
    on_message: InboundHandler,
) -> int:
    """Drain any wake-queue rows for our slug. Returns count of messages replayed."""
    if not cfg.wake_messages_table:
        return 0

    loop = asyncio.get_running_loop()
    ddb = boto3.resource("dynamodb", region_name=cfg.aws_region)
    table = ddb.Table(cfg.wake_messages_table)

    now_unix = int(time.time())
    stale_threshold = now_unix - WAKE_CLAIM_STALE_SECONDS

    def _claim() -> dict[str, Any] | None:
        # DDB UpdateItem is an upsert: without an explicit existence guard, a
        # missing row would be created with just {slug, claimed_at} (no
        # messages). attribute_exists(slug) forces the claim to fail when
        # there's no real wake-queue row to drain.
        try:
            resp = table.update_item(
                Key={"slug": cfg.agent_slug},
                UpdateExpression="SET claimed_at = :now",
                ConditionExpression=(
                    "attribute_exists(slug) AND "
                    "(attribute_not_exists(claimed_at) OR claimed_at < :stale)"
                ),
                ExpressionAttributeValues={
                    ":now": now_unix,
                    ":stale": stale_threshold,
                },
                ReturnValues="ALL_NEW",
            )
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            if code == "ConditionalCheckFailedException":
                log.info("wake claim skipped: no wake row, or claimed within stale window")
                return None
            raise
        return _decimal_to_native(resp.get("Attributes") or {})

    item = await loop.run_in_executor(None, _claim)
    if not item:
        return 0

    messages = item.get("messages") or []
    if not messages and item.get("message"):
        # Legacy single-message shape.
        messages = [{
            "text": item.get("message"),
            "sender_name": item.get("sender_name", "user"),
            "timestamp": item.get("timestamp"),
        }]
    if not messages:
        log.info("wake row claimed but had no messages; deleting")
        await _delete_claimed(loop, table, cfg.agent_slug, now_unix)
        return 0

    log.info("wake replay: %d queued message(s) for %s", len(messages), cfg.agent_slug)
    replayed = 0
    for raw in messages:
        text = (raw.get("text") or "").strip()
        if not text:
            continue
        sender = raw.get("sender_name") or "user"
        peer_id = str(raw.get("peer_id") or _peer_from_config(cfg))
        if not peer_id:
            log.warning("wake replay: no peer_id available; skipping message")
            continue
        try:
            await on_message(InboundMessage(
                peer_id=peer_id,
                sender_name=sender,
                text=text,
                channel=cfg.channel,
            ))
            replayed += 1
        except Exception:
            log.exception("wake replay handler failed; aborting drain to allow next-boot retry")
            return replayed

    await _delete_claimed(loop, table, cfg.agent_slug, now_unix)
    return replayed


def _peer_from_config(cfg: Config) -> str:
    raw = cfg.channel_config.get("allowFrom") or []
    if not raw:
        return ""
    first = str(raw[0])
    if first.startswith("user:"):
        first = first[len("user:"):]
    return first


async def _delete_claimed(
    loop: asyncio.AbstractEventLoop,
    table: Any,
    slug: str,
    claim_ts: int,
) -> None:
    def _go() -> None:
        try:
            table.delete_item(
                Key={"slug": slug},
                ConditionExpression="claimed_at = :ts",
                ExpressionAttributeValues={":ts": claim_ts},
            )
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            if code == "ConditionalCheckFailedException":
                log.info("wake delete skipped: claim timestamp changed (re-claimed elsewhere)")
                return
            raise

    try:
        await loop.run_in_executor(None, _go)
    except Exception:
        log.exception("wake delete failed; row will remain until TTL or next-boot stale-claim")
