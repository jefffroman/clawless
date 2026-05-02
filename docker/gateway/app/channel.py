"""Channel adapter protocol + TelegramChannel implementation.

The protocol is intentionally tiny: ``send`` (outbound), ``run`` (inbound
event loop), ``shutdown``. v1 will add DiscordChannel and SlackChannel
implementing the same interface.

TelegramChannel uses aiogram 3.x (asyncio-native). Long-polling with
``drop_pending_updates=True``: the wake_listener Lambda is the canonical
queue for messages received while the gateway was asleep, so we don't need
Telegram to replay them; the gateway boot path drains DynamoDB instead.

Message length: Telegram caps inbound HTTP payloads at 4096 chars. We chunk
on ``\\n\\n`` boundaries with a soft cap that leaves headroom for UTF-8
multibyte glyphs and reply formatting.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Protocol

from contextlib import asynccontextmanager
from typing import AsyncContextManager

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import Message
from aiogram.utils.chat_action import ChatActionSender

from .config import TELEGRAM_CHUNK_MAX, Config

log = logging.getLogger("clawless.channel")


# Inbound handler signature — handed to the channel by main.py.
InboundHandler = Callable[["InboundMessage"], Awaitable[None]]


class InboundMessage:
    __slots__ = ("peer_id", "sender_name", "text", "channel")

    def __init__(self, *, peer_id: str, sender_name: str, text: str, channel: str) -> None:
        self.peer_id = peer_id
        self.sender_name = sender_name
        self.text = text
        self.channel = channel


class Channel(Protocol):
    name: str

    async def start(self, on_message: InboundHandler) -> None: ...
    async def send(self, peer_id: str, text: str) -> None: ...
    async def shutdown(self) -> None: ...
    def typing(self, peer_id: str) -> AsyncContextManager[None]:
        """Async context manager that shows a 'typing' indicator while open.

        Implementations should renew the indicator periodically so it stays
        visible across multi-second turns (Bedrock + tool loops + compaction).
        """
        ...


@asynccontextmanager
async def _no_typing(_peer_id: str):
    """Fallback CM used by channels that don't support typing indicators."""
    yield


def chunk_for_telegram(text: str, limit: int = TELEGRAM_CHUNK_MAX) -> list[str]:
    """Split ``text`` so each chunk fits inside ``limit`` chars, preferring
    paragraph (\\n\\n) and line (\\n) boundaries.
    """
    if len(text) <= limit:
        return [text] if text else []
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = remaining.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


class TelegramChannel:
    name = "telegram"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        token = cfg.channel_config.get("botToken") or cfg.channel_config.get("token")
        if not token:
            raise SystemExit("CLAWLESS_CHANNEL_CONFIG.botToken missing for telegram channel")
        self.bot = Bot(token=token, default=DefaultBotProperties(parse_mode=None))
        self.dispatcher = Dispatcher()
        self.allow_from = self._normalize_allowlist(cfg.channel_config.get("allowFrom") or [])
        self._poll_task: asyncio.Task[None] | None = None
        self._on_message: InboundHandler | None = None

    @staticmethod
    def _normalize_allowlist(raw: list[Any]) -> set[str]:
        # Telegram peer IDs come through as ints; allowFrom may store them as
        # ints, strings, or "user:<id>" prefixed strings. Normalize to plain
        # string ints for comparison.
        out: set[str] = set()
        for item in raw:
            s = str(item).strip()
            if s.startswith("user:"):
                s = s[len("user:"):]
            if s:
                out.add(s)
        return out

    async def start(self, on_message: InboundHandler) -> None:
        self._on_message = on_message

        @self.dispatcher.message()
        async def _handle(message: Message) -> None:  # type: ignore[unused-variable]
            await self._handle_message(message)

        # The wake_listener Lambda installs a webhook on this bot when the
        # gateway is asleep. Telegram refuses getUpdates while a webhook is
        # active, so we must clear it explicitly before long-polling. aiogram's
        # start_polling does not auto-delete it on its own.
        log.info("clearing wake_listener webhook before long-polling")
        try:
            await self.bot.delete_webhook(drop_pending_updates=True)
        except Exception:
            log.exception("delete_webhook failed (continuing — start_polling may retry)")

        # Drop pending: the wake_listener queue + DynamoDB is the authoritative
        # source for messages received while we were asleep. Long-poll only
        # handles fresh inbound from now on.
        log.info("starting telegram long-polling for %s", self.cfg.agent_slug)
        self._poll_task = asyncio.create_task(
            self.dispatcher.start_polling(self.bot, handle_signals=False, drop_pending_updates=True),
            name="telegram-polling",
        )

    async def _handle_message(self, message: Message) -> None:
        if message.text is None:
            return
        peer = message.chat.id if message.chat else None
        if peer is None:
            return
        peer_str = str(peer)
        if self.allow_from and peer_str not in self.allow_from:
            log.warning("rejecting message from non-allowlisted peer %s", peer_str)
            return
        sender_name = "user"
        if message.from_user:
            sender_name = message.from_user.full_name or message.from_user.username or sender_name
        if self._on_message is None:
            return
        try:
            await self._on_message(InboundMessage(
                peer_id=peer_str,
                sender_name=sender_name,
                text=message.text,
                channel=self.name,
            ))
        except Exception:
            log.exception("inbound handler raised")

    async def send(self, peer_id: str, text: str) -> None:
        if not text.strip():
            return
        for chunk in chunk_for_telegram(text):
            try:
                await self.bot.send_message(int(peer_id), chunk)
            except (TypeError, ValueError):
                await self.bot.send_message(peer_id, chunk)
            except Exception:
                log.exception("telegram send failed")
                raise

    def typing(self, peer_id: str) -> AsyncContextManager[None]:
        """ChatActionSender re-sends 'typing' every ~5 s until the context
        exits, so the indicator stays visible through long Bedrock streams
        and tool-use loops."""
        try:
            chat_id: int | str = int(peer_id)
        except (TypeError, ValueError):
            chat_id = peer_id
        return ChatActionSender.typing(bot=self.bot, chat_id=chat_id)

    async def shutdown(self) -> None:
        log.info("shutting down telegram channel")
        try:
            await self.dispatcher.stop_polling()
        except Exception:
            log.exception("dispatcher.stop_polling raised")
        if self._poll_task is not None:
            try:
                await asyncio.wait_for(self._poll_task, timeout=5)
            except (asyncio.TimeoutError, Exception):
                self._poll_task.cancel()
        try:
            await self.bot.session.close()
        except Exception:
            pass


def build_channel(cfg: Config) -> Channel:
    if cfg.channel == "telegram":
        return TelegramChannel(cfg)
    raise SystemExit(f"channel {cfg.channel!r} is not supported in v0 (telegram only)")
