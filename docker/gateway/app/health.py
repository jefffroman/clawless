"""Health endpoint on 127.0.0.1:18789. Loopback only — there's no LB in
front of the Fargate task, so binding publicly would be misleading.
"""

from __future__ import annotations

import logging

from aiohttp import web

from .config import HEALTH_HOST, HEALTH_PORT, Config

log = logging.getLogger("clawless.health")


class HealthServer:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._ready = False
        self._app = web.Application()
        self._app.router.add_get("/health", self._handle)
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    async def _handle(self, _request: web.Request) -> web.Response:
        return web.json_response({
            "ok": self._ready,
            "agent_slug": self.cfg.agent_slug,
            "channel": self.cfg.channel,
        })

    def mark_ready(self) -> None:
        self._ready = True

    async def start(self) -> None:
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, HEALTH_HOST, HEALTH_PORT)
        await self._site.start()
        log.info("health endpoint listening on %s:%d", HEALTH_HOST, HEALTH_PORT)

    async def stop(self) -> None:
        if self._site is not None:
            await self._site.stop()
        if self._runner is not None:
            await self._runner.cleanup()
