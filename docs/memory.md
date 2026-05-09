# Memory, Compaction, and Flush

The clawless gateway runs three intertwined subsystems against the agent's
workspace markdown files: a hybrid retrieval index, a transcript compactor,
and a memory-flush turn. Together they keep the agent's context useful and
its durable knowledge searchable across sleep/wake cycles.

This page documents how those pieces interact, what triggers each, and the
tunable knobs operators can adjust.

## File layout

Everything lives under `${WORKSPACE_DIR}` (default `/home/clawless/`),
which is synced to S3 on SIGTERM and back down on boot.

```
${WORKSPACE_DIR}/
├── memory/
│   ├── MEMORY.md             — main long-term store (curated by the agent)
│   ├── SOUL.md, IDENTITY.md, USER.md, AGENTS.md, …  — themed memory files
│   ├── 2026-05-09.md         — daily archives (created by the flush turn)
│   ├── .flush_state.json     — per-session high-water mark for incremental flush
│   ├── chroma_db/            — (data dir, ephemeral; lives at MEMORY_DATA_DIR)
│   ├── bm25_corpus.json
│   ├── memory_graph.json
│   └── sync_state.json
└── transcripts/
    └── telegram_<peer>.jsonl  — per-session transcript
```

> **Note**: `MEMORY_DATA_DIR` (default `/var/lib/clawless-memory`) holds
> the chromadb collection plus the BM25/graph/sync sidecars. It's outside
> the workspace and ephemeral per container — rebuilt on every boot via
> `reindex_if_stale`. The state file `sync_state.json` *does* live there
> (also ephemeral), but the SHA-mapping stays consistent because reindex
> regenerates it from the workspace markdown on boot.

## Retrieval

Before every Bedrock turn, `MemoryIndex.retrieve_markdown(query)` runs a
hybrid query:

1. **BM25** lexical scoring against `bm25_corpus.json`
2. **Vector** ANN over the chromadb collection (MiniLM-L6-v2 via ONNX)
3. **Reciprocal Rank Fusion** to merge the two ranked lists
4. **Knowledge graph** lookup via `memory_graph.json` (cross-section
   "mentions" edges built at index time)

The fused top-N chunks plus graph neighbors are formatted as a markdown
block and prepended to the prompt as `## Auto-Retrieved Memory Context`.
The agent does not search memory manually — relevant chunks are already
in its context.

## Reindex (SHA-mapped, incremental)

Source files are tracked by **per-file SHA1** (stored as a dict in
`sync_state.json`). When the index needs refreshing, `needs_reindex`
returns `(changed, removed)` source-key lists; `do_reindex` deletes only
changed/removed sources from the chromadb collection and upserts only the
changed ones. Chunk IDs are enumerated **per-source** (`{source}:{i}`)
so adding a section to one file never invalidates other files' IDs.

Reindex runs in three places:

| Trigger | Where | Notes |
|---|---|---|
| Boot | `main._main` | Eager full check after warmup; idempotent |
| Sleep tool | `tools._run_sleep` | Before SFN trigger, after the pre-sleep flush has appended new content |
| After flush | `memory_flush.flush_then_reindex` | Picks up newly-flushed daily-note content |

A periodic standalone reindex loop is **not** needed — every flush
triggers a reindex, and flush itself is the only way new searchable
content lands on disk.

## Compaction (mid-session, async)

When a session's transcript exceeds `mid_session_token_threshold` (default
**96 000** — generous because Bedrock-Haiku with prompt caching handles
large context cheaply), a background task spawns:

```
agent._process()
  ├── append user turn
  ├── if will_mid_session_compact(turns):
  │     spawn _run_bg_compaction(snapshot)
  └── continue: bedrock.run_turn → reply to user
```

The background task does, in order:
1. **flush_then_reindex (reason=pre-compact)** — captures durable knowledge
   from the snapshot before older turns are summarized away
2. **`run_mid_session_compact_async`** — splits the snapshot at a tool-pair
   boundary, sends "One moment…" to the channel, summarizes the older half
   via Nova Micro, then takes the per-session lock and atomic-swaps the
   live transcript (re-reads under lock to preserve any user turns that
   arrived during summarize)
3. **`run_hard_reset`** — if the post-swap transcript still exceeds
   `hard_ceiling_tokens` (default **150 000**, leaving 50K under
   Bedrock's 200K input cap), replaces the transcript with just the
   recap turn. Hard-reset does NOT re-flush — pre-compact already covered
   this cycle.

The user-reply path proceeds without waiting on this task. Mid-session
compaction is invisible to the user except for the "One moment…" status
notice that fires once when it triggers.

The boot-time idle recap is unchanged — when the prior session's last turn
is older than `IDLE_RECAP_SECONDS` (default 1 h), it summarizes synchronously
during boot and prepends the recap to the next prompt as
`## Last Session Recap`.

## Flush (incremental, lock-guarded)

A **flush turn** is the agent talking to its primary model with a synthetic
`append_file memory/YYYY-MM-DD.md` instruction. It captures durable
knowledge into a daily-note file that the next reindex picks up. The flush
turn's reply text is discarded — the side effect on disk is what matters.

### Triggers

| Trigger | Where | Reason label |
|---|---|---|
| Pre-compact | `agent._run_bg_compaction` | `pre-compact` |
| Pre-sleep | `tools._sleep_with_flush` wrapper around the sleep tool | `pre-sleep` |
| Periodic-growth | `main._maintenance_loop` (every `maintenance_interval_s`) | `periodic-growth` |

Periodic-growth fires per session whose `tokens-since-last-flush` exceeds
`periodic_growth_threshold` (default **8 000**). Sessions below the
threshold are skipped silently.

### Incremental window

`agent._last_flush_ts: dict[str, str]` records, per session, the ISO ts of
the newest turn included in the most recent successful flush. Persisted
to `memory/.flush_state.json` (which is in the synced workspace, so it
survives sleep/wake).

Each flush filters the transcript to turns with `ts > since_ts` before
sending to the model. After a successful flush, the high-water mark
advances to the newest evaluated turn's ts.

On first deployment, sessions present on disk get their last-turn ts
written to `_last_flush_ts` so historical content is treated as already
flushed (avoiding a one-time massive flush of pre-existing transcripts).

### Per-session lock

`agent._flushing_sids: set[str]` prevents concurrent flushes for the same
session. If a triggered flush finds its sid already present, it logs and
returns immediately — flushes are deduplicated, not queued.

This bounds nested invocations: if the flush turn's model itself emits a
tool call that loops back into the flush trigger (e.g., the model picks
the `sleep` tool from within a flush), the wrapper's nested call sees the
lock held and skips, terminating the recursion at depth 1.

### Order of operations on sleep

```
agent.handle_inbound (user says "sleep")
  → bedrock.run_turn (outer)
    → model emits toolUse: sleep
    → tools._sleep_with_flush wrapper:
        1. agent.flush_all_sessions_before_sleep()
           → for sid in known_session_ids():
              agent.flush_session(sid, "pre-sleep")
                → flush_then_reindex
                   → run_memory_flush (one Bedrock turn, append_file)
                   → memory.reindex_if_stale (incremental)
        2. inner _run_sleep:
           → memory.reindex_if_stale (defensive no-op if flush already reindexed)
           → SSM /active=false; SFN trigger
  → entrypoint sync_up to S3 on SIGTERM
```

Sleep tool latency is roughly **flush turn (~5–10 s) + reindex (~100 ms)
+ SFN trigger (~50 ms)**. The flush latency is the dominant cost; it's
acceptable on explicit sleep events because durable knowledge is
guaranteed to land in S3 before the workspace syncs up.

## Tuning knobs (env vars)

All thresholds are env-overridable on the Fargate task. The agent reads
them via `config.load()` at boot.

| Env var | Default | Meaning |
|---|---|---|
| `CLAWLESS_MID_SESSION_TOKEN_THRESHOLD` | `96000` | Trigger mid-session compaction above this |
| `CLAWLESS_HARD_CEILING_TOKENS` | `150000` | Trigger hard-reset (recap-only) above this after compaction |
| `CLAWLESS_MAINTENANCE_INTERVAL_S` | `300` (testing) / `1800` (prod) | Maintenance loop check cadence |
| `CLAWLESS_PERIODIC_GROWTH_THRESHOLD` | `8000` | Tokens of growth-since-last-flush required to fire periodic flush |
| `CLAWLESS_COMPACTION_MODEL` | `us.amazon.nova-micro-v1:0` | Model used for compaction summary (cheap, non-tool) |
| `CLAWLESS_MODEL` | per-agent SSM | Primary model — also used for flush turns |
| `CLAWLESS_VERBOSE` | `false` | Raise gateway logger to DEBUG |

> The default `MAINTENANCE_INTERVAL_S=300` ships during the
> initial-validation phase for easy live testing. After live behavior is
> confirmed it should revert to `1800` (30 min) to limit cost — periodic
> flush turns use the primary model with the full tool registry.

## Diagnostic log lines

Useful greps when investigating memory/flush behavior:

| Pattern | What it means |
|---|---|
| `full reindex:` | First-boot rebuild (legacy state file) |
| `incremental reindex: +N changed -M removed` | SHA-mapping picked up specific files |
| `memory flush starting (reason=…)` | Flush turn began for one session |
| `memory flush done (reason=…)` | Flush completed (regardless of whether the agent appended) |
| `flush skipped (reason=…): another flush in flight` | Per-session lock deduplicated a triggered flush |
| `background compaction: summarizing N older turns` | Mid-session compaction's summarize call |
| `background compaction swapped: N -> M turns` | Atomic swap completed |
| `post-compaction over hard ceiling … hard-reset` | Hard-reset path fired |
| `idle recap: session … last activity … ago` | Boot-time recap fired |
| `[client/agent] periodic flush+reindex triggered (growth=N tokens)` | Maintenance loop fired for a session |

## See also

- `docker/gateway/app/memory.py` — index implementation
- `docker/gateway/app/memory_flush.py` — flush turn runner
- `docker/gateway/app/compaction.py` — compaction + hard-reset
- `docker/gateway/app/agent.py` — orchestration (per-sid locks, flush state, bg tasks)
- [troubleshooting.md](troubleshooting.md) — operational debugging
