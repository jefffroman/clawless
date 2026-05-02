"""clawless-gateway entry point.

Boot order:

1. Load env config.
2. Warm chromadb / embedder.
3. Initial memory reindex (under lock).
4. Eager idle-recap of any pre-existing transcripts.
5. Start /health endpoint (binds 127.0.0.1:18789, /health returns ok=true).
6. Replay queued wake messages from DynamoDB (claim-deliver-delete).
7. Start telegram long-polling.
8. Reindex loop runs forever in the background.

Shutdown (SIGTERM/SIGINT):

1. Stop telegram polling.
2. Install wake_listener webhook (so messages during sync_up route to the
   Lambda, not a dying gateway).
3. Stop /health.
4. Cancel reindex loop.
5. Exit cleanly. The shell entrypoint owns sync_up to S3 after we exit.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from typing import Any

from . import config as config_module
from .agent import Agent
from .bedrock import BedrockClient
from .channel import build_channel
from .health import HealthServer
from .memory import MemoryIndex, reindex_loop
from .tools import build_registry
from .transcript import TranscriptStore
from .wake_greet import wake_greet
from .webhook import install_wake_listener_webhook


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    # boto noise
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("aiogram").setLevel(logging.INFO)


async def _main() -> int:
    cfg = config_module.load()
    _configure_logging(cfg.verbose)
    log = logging.getLogger("clawless.main")

    log.info(
        "starting clawless-gateway: agent=%s channel=%s model=%s",
        cfg.agent_slug, cfg.channel, cfg.model_id,
    )

    os.makedirs(cfg.memory_source_dir, exist_ok=True)
    os.makedirs(cfg.transcripts_dir, exist_ok=True)

    memory = MemoryIndex(
        source_dir=cfg.memory_source_dir,
        data_dir=cfg.memory_data_dir,
        slug_safe=cfg.slug_safe,
    )
    bedrock = BedrockClient(cfg)
    tools = build_registry(cfg)
    transcripts = TranscriptStore(cfg.transcripts_dir)
    channel = build_channel(cfg)

    agent = Agent(
        cfg=cfg,
        bedrock=bedrock,
        memory=memory,
        tools=tools,
        transcripts=transcripts,
        channel=channel,
    )

    health = HealthServer(cfg)
    await health.start()

    # Warm + initial reindex (eager, before health-ready and before going live).
    await memory.warmup_async()
    try:
        result = await memory.reindex_if_stale()
        log.info("initial reindex: %s", result)
    except Exception:
        log.exception("initial reindex failed; continuing")

    # Eager idle recap for any sessions older than 1 h.
    try:
        await agent.boot_recap_known_sessions()
    except Exception:
        log.exception("boot idle-recap pass failed; continuing")

    # Drain wake queue (or fire a synthetic greeting if empty) before going
    # live so the user's queued message is the first turn after wake — and so
    # operator-initiated wakes still surface a "Hello <agent>" reply.
    try:
        dispatched = await wake_greet(cfg=cfg, on_message=agent.handle_inbound)
        if dispatched:
            log.info("wake-greet dispatched %d message(s)", dispatched)
    except Exception:
        log.exception("wake-greet failed; continuing without it")

    # Start channel last, after all init is done.
    await channel.start(agent.handle_inbound)
    health.mark_ready()

    reindex_task = asyncio.create_task(reindex_loop(memory), name="reindex-loop")

    stop_event = asyncio.Event()

    def _request_stop(_sig: int = 0, _frame: Any = None) -> None:
        log.info("shutdown signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            signal.signal(sig, _request_stop)

    await stop_event.wait()
    log.info("shutting down")

    reindex_task.cancel()
    try:
        await reindex_task
    except (asyncio.CancelledError, Exception):
        pass

    # Stop polling first so no new turns start, then flip the webhook before
    # the shell's sync_up runs.
    try:
        await channel.shutdown()
    except Exception:
        log.exception("channel shutdown raised")

    try:
        await install_wake_listener_webhook(cfg)
    except Exception:
        log.exception("webhook install raised")

    try:
        await health.stop()
    except Exception:
        log.exception("health stop raised")

    log.info("clean exit")
    return 0


def main() -> int:
    try:
        return asyncio.run(_main())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
