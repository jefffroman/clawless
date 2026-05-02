"""Webhook handover on shutdown — flips Telegram from long-poll back to the
wake_listener Lambda's webhook so messages received during sync_up route to
the wake queue, not to a dying gateway.

The order in the SIGTERM trap matters: setWebhook must complete *before*
sync_up to S3, otherwise messages sent in the (potentially multi-second)
sync_up window are lost.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from .config import Config

log = logging.getLogger("clawless.webhook")


def _slug_safe(slug: str) -> str:
    return slug.replace("/", "-")


async def install_wake_listener_webhook(cfg: Config) -> None:
    """Install the wake_listener Function URL as the Telegram webhook for this bot."""
    if cfg.channel != "telegram":
        return
    if not cfg.wake_listener_url:
        log.info("webhook handover skipped: WAKE_LISTENER_URL unset")
        return

    bot_token = cfg.channel_config.get("botToken") or cfg.channel_config.get("token")
    if not bot_token:
        log.warning("webhook handover skipped: no botToken in channel_config")
        return

    api = f"https://api.telegram.org/bot{bot_token}/setWebhook"
    body = urllib.parse.urlencode({
        "url": cfg.wake_listener_url,
        "secret_token": _slug_safe(cfg.agent_slug),
        "allowed_updates": json.dumps(["message"]),
    }).encode("utf-8")

    def _post() -> str:
        req = urllib.request.Request(
            api,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.URLError as e:
            return f"<error: {e}>"

    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(None, _post)
        log.info("setWebhook → wake_listener: %s", result[:200])
    except Exception:
        log.exception("setWebhook failed (non-fatal during shutdown)")
