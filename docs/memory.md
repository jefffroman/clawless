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
├── memory/                   — AUTHORITATIVE source markdown
│   ├── MEMORY.md             — main long-term store (curated by the agent)
│   ├── SOUL.md               — persona: identity + character (persona-seeded)
│   ├── USER.md, …            — themed memory files
│   ├── 2026-05-09.md         — daily archives (created by the flush turn)
│   └── .flush_state.json     — per-session high-water mark for incremental flush
├── .index/                   — persisted index cache (= MEMORY_DATA_DIR)
│   ├── vstore.npz            — int8 vector store (ids + q + scale)
│   ├── bm25_corpus.json
│   ├── memory_graph.json
│   └── sync_state.json       — per-file SHA map + the reindex commit token
└── transcripts/
    └── telegram_<peer>.jsonl  — per-session transcript
```

> **Source-of-truth contract.** The markdown under `memory/` is
> **authoritative**. `.index/` (`MEMORY_DATA_DIR`, default
> `$WORKSPACE_DIR/.index`) is a *persisted cache* that rides **inside** the
> single workspace archive across sleep/wake — it is **not** rebuilt on every
> boot. A normal wake trusts the restored index and does zero index work;
> only a true first boot (no `.index`) builds synchronously. The index is
> reconciled to the markdown by per-file SHA whenever a reindex runs (see
> Reindex below); `sync_state.json` is written **last** as the commit token,
> so a crash mid-write is self-corrected by the next reindex's SHA compare.
> Only chromadb's bundled ONNX embedder is used — vectors are persisted
> ourselves as a compact int8 matrix (chromadb's PersistentClient is unused).

## Personas

An agent ships as a pre-formed **persona**, selected by its name. `seed.tf`
normalizes the effective agent name to a `persona_key`
(`lower`, then `[^a-z0-9_-]` → `-`) and seeds `SOUL.md` from
`tofu/modules/client/seed/personas/<persona_key>/SOUL.md.tftpl`. A persona may
also override `MEMORY.md`/`USER.md`; anything it doesn't ship falls back to
the generic scaffold in `seed/`.

There is **no generic SOUL** — an unknown persona fails `tofu plan` early via a
resource precondition. Persona resolves at agent creation only (seed objects
are write-once via `ignore_changes`). Authoring guide and the content rule
(no infrastructure/mechanism in any client-reachable file):
`tofu/modules/client/seed/personas/README.md`.

## Retrieval

Before every Bedrock turn, `MemoryIndex.retrieve_markdown(query)` runs a
hybrid query:

1. **BM25** lexical scoring against `bm25_corpus.json`
2. **Vector** brute-force squared-L2 top-k over the persisted int8 store
   (`vstore.npz`); embeddings via the bundled ONNX MiniLM-L6-v2
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
returns `(changed, removed)` source-key lists; `do_reindex` re-embeds only
the changed sources and **reuses prior int8 rows by chunk-id** for
everything unchanged (removed sources' chunks are simply absent from the
rebuilt store). Chunk IDs are enumerated **per-source** (`{source}:{i}`)
so adding a section to one file never invalidates other files' IDs, and
the int8 store + `bm25_corpus.json` are rebuilt from the same chunk list
in one locked pass so they can never disagree on the id set. Write order
is `vstore.npz` → `bm25_corpus.json` → `memory_graph.json` →
`sync_state.json` (the commit token, last).

Because the index is persisted in the archive, reindex is **consolidated
at sleep**, not run on every wake:

| Trigger | Where | Notes |
|---|---|---|
| First boot only | `main._main` | Synchronous full build *iff* no persisted `.index` (true first boot). Every other wake skips reindex and trusts the restored index. |
| Shutdown (SIGTERM) | `main._main` shutdown handler | The one chokepoint **all** sleeps funnel through (self-sleep via the `sleep` tool *and* operator/idle pause both arrive as SIGTERM). Best-effort, incremental, after the channel is down. |
| After flush | `memory_flush.flush_then_reindex` | Picks up newly-flushed daily-note content during a live session |

The only gap is a **Python self-crash** (not a graceful SIGTERM) that
coincides with a direct agent edit to `memory/*.md` since the last
reindex: the shell still snapshots, so the restored index lags the
markdown. This is non-catastrophic — markdown is authoritative, the agent
can `read_file` it — and self-heals at the next flush / periodic
maintenance reindex; meanwhile `_sync_status` surfaces an `OUT_OF_SYNC`
banner in the retrieved memory block.

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
