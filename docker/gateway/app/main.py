"""clawless-gateway entry point.

Boot order:

1. Load env config.
2. Warm the embedder + load the persisted int8 vector store.
3. Reindex ONLY on a true first boot (no persisted .index). Every other
   wake trusts the restored index — reindex is done at sleep (the SIGTERM
   shutdown handler), not on the wake critical path.
4. Eager idle-recap of any pre-existing transcripts.
5. Start /health endpoint (binds 127.0.0.1:18789, /health returns ok=true).
6. Replay queued wake messages from DynamoDB (claim-deliver-delete).
7. Start telegram long-polling.
8. Maintenance loop runs forever in the background — every
   ``maintenance_interval_s`` it fires flush_then_reindex for sessions
   whose growth-since-last-flush exceeds ``periodic_growth_threshold``.

Shutdown (SIGTERM/SIGINT) — the one chokepoint every sleep funnels through
(self-sleep and operator/idle pause both end here):

1. Cancel maintenance loop.
2. Stop telegram polling (no new turns can write markdown after this).
3. Reindex (best-effort, incremental) so the snapshot is index-consistent
   regardless of how the agent was put to sleep.
4. Install wake_listener webhook (so messages during sync_up route to the
   Lambda, not a dying gateway).
5. Stop /health.
6. Exit cleanly. The shell entrypoint owns sync_up to S3 after we exit; if
   ECS SIGKILLs us mid-reindex, snapshot (which runs after) simply doesn't,
   and the next boot restores the prior consistent archive.
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
from .memory import MemoryIndex
from .tools import build_registry
from .transcript import TranscriptStore
from .wake_greet import wake_greet
from .webhook import install_wake_listener_webhook


async def _maintenance_loop(agent: Agent) -> None:
    """Periodic flush_then_reindex for sessions with token-growth past
    threshold. Runs forever until cancelled.

    Each tick: for each known session, if growth-since-last-flush exceeds
    ``periodic_growth_threshold``, fire ``agent.flush_session`` (which
    handles the per-sid lock and incremental window).
    """
    cfg = agent.cfg
    log_main = logging.getLogger("clawless.maintenance")
    while True:
        try:
            await asyncio.sleep(cfg.maintenance_interval_s)
            for sid in agent.known_session_ids():
                growth = agent.session_growth(sid)
                if growth < cfg.periodic_growth_threshold:
                    continue
                log_main.info(
                    "[%s] periodic flush+reindex triggered (growth=%d tokens)",
                    sid, growth,
                )
                await agent.flush_session(sid, reason="periodic-growth")
        except asyncio.CancelledError:
            raise
        except Exception:
            log_main.exception("maintenance loop iteration failed")


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
    tools = build_registry(cfg, memory)
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

    # Auto pre-sleep flush: wrap the sleep tool's run so it always captures
    # durable session knowledge into the daily note before sleeping,
    # regardless of whether the agent chose to write it itself. The flush
    # itself reindexes (memory_flush.flush_then_reindex); the authoritative
    # index reconciliation happens later in the SIGTERM shutdown handler.
    sleep_tool = tools.get("sleep")
    if sleep_tool is not None:
        _inner_sleep_run = sleep_tool.run

        async def _sleep_with_flush(args: dict[str, Any]) -> str:
            try:
                await agent.flush_all_sessions_before_sleep()
            except Exception:
                log.exception("pre-sleep flush wrapper failed; continuing with sleep")
            return await _inner_sleep_run(args)

        sleep_tool.run = _sleep_with_flush

    health = HealthServer(cfg)
    await health.start()

    # Warm the embedder + load the persisted store. Reindex is consolidated
    # at the SIGTERM shutdown handler, so a normal wake does NO index work —
    # it trusts the index restored from the archive. Only a true first boot
    # (no persisted .index) builds synchronously here, before going live.
    await memory.warmup_async()
    if memory.has_persisted_index():
        log.info("persisted index present; skipping wake reindex")
    else:
        try:
            result = await memory.reindex_if_stale()
            log.info("first-boot reindex: %s", result)
        except Exception:
            log.exception("first-boot reindex failed; continuing")

    # Eager idle recap for any sessions older than 1 h.
    try:
        await agent.boot_recap_known_sessions()
    except Exception:
        log.exception("boot idle-recap pass failed; continuing")

    # Initialize incremental-flush high-water marks for any pre-existing
    # sessions on disk. Sessions without an entry in flush_state.json get
    # their last-turn ts written, so historical content is treated as
    # already-flushed and the next periodic pass doesn't trigger a massive
    # one-time flush of pre-deployment transcripts.
    try:
        agent.init_flush_state_for_existing()
    except Exception:
        log.exception("flush-state init failed; continuing")

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

    maintenance_task = asyncio.create_task(
        _maintenance_loop(agent), name="maintenance-loop",
    )

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

    maintenance_task.cancel()
    try:
        await maintenance_task
    except (asyncio.CancelledError, Exception):
        pass

    # Stop polling first so no new turns start, then flip the webhook before
    # the shell's sync_up runs.
    try:
        await channel.shutdown()
    except Exception:
        log.exception("channel shutdown raised")

    # Reindex here — the one chokepoint every sleep path funnels through
    # (self-sleep via the sleep tool and operator/idle pause both arrive as
    # SIGTERM). Channel is down so no turn can still mutate markdown. It's
    # incremental (only files changed since the last reindex), so it's cheap
    # even for a large corpus. Best-effort: a SIGKILL before the shell's
    # snapshot just leaves the prior consistent archive for the next boot.
    try:
        result = await memory.reindex_if_stale()
        log.info("shutdown reindex: %s", result)
    except Exception:
        log.exception("shutdown reindex failed; prior index will be snapshotted")

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
