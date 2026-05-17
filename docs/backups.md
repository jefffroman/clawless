# Backups & Restore

## Overview

Every active agent persists its entire workspace as **one versioned S3
object** — `agents/{client}/{agent}/workspace.tar.zst` (tar + zstd-1). The
object is extracted on boot and re-snapshotted on shutdown (SIGTERM). All
agents share a single versioned bucket with cross-region replication. When an
agent is removed, its archive is copied to a single `removed/…` key before
teardown.

History is **S3 object versions of that one key** — there are no dated keys
and no per-file fan-out. A bounded key set is what lets the bucket lifecycle
actually expire old state (unbounded distinct keys, not version bloat, were
the prior accumulation problem).

| Component | Detail |
|-----------|--------|
| Source | `$WORKSPACE_DIR` (default `/home/clawless/`) in each gateway container |
| Bucket | `clawless-backups-{account}` (us-east-1) |
| Key | `agents/{client}/{agent}/workspace.tar.zst` (one versioned object) |
| Replica | `clawless-backups-replica-{account}` (us-east-2, STANDARD_IA) |
| Trigger | Container boot (restore) and SIGTERM (snapshot) — see `entrypoint.sh` |
| Versioning | Enabled — current kept indefinitely, 2 noncurrent versions kept 7 days |
| Encryption | AES-256 (SSE-S3) |

## How it works

The gateway container's `entrypoint.sh` handles persistence in two places:

1. **`restore()`** — runs at boot. Ordered fallback: (a) the current
   `workspace.tar.zst` (verified with `zstd -t` + `tar -t`, then extracted
   atomically via a staging dir); (b) its immediately-prior S3 version if the
   current one is missing/corrupt; (c) on the very first boot only, before any
   archive exists, a per-file sync from the `agents/{slug}/workspace/` seed
   prefix that `seed.tf` wrote; (d) nothing usable anywhere → hard error.
2. **`snapshot()`** — runs on SIGTERM (sleep or task stop). Tars
   `$WORKSPACE_DIR` (excluding `.cache/` and `.aws/`), pipes through `zstd -1`,
   and PUTs it as a new version of the single key with one `aws s3 cp`
   (retried once). The object lands whole or not at all — atomic, unlike the
   old non-atomic per-file `sync --delete`.

There is no periodic timer — persistence happens at lifecycle boundaries
(boot and shutdown). The Fargate task's SIGTERM handler (`shutdown()`) calls
`snapshot` before exiting. Snapshot failure is non-fatal (sleep must not
hang) — the prior S3 version remains and the failure is logged for alerting.

## Retention

Unchanged from the per-file model — the lifecycle rule has no prefix filter
and applies to every object:

| Layer | Retention |
|-------|-----------|
| Current version | Indefinite |
| Noncurrent versions | 2 most recent kept, deleted after 7 days |
| Expired delete markers | Auto-cleaned |
| Replica (us-east-2) | Same versioning; 1 noncurrent kept 3 days |

Because there is no current-object expiration, the current version of each
key (including a `removed/…` archive) is kept indefinitely. The 7-day /
2-version limit only bites *noncurrent* versions, which for a given agent
exist only across repeated sleep/wake (or repeated remove→re-add→remove).

## On removal

When an agent is removed, the Lambda copies its archive
`agents/{slug}/workspace.tar.zst` → `removed/{slug}/workspace.tar.zst`
(overwrite ⇒ a new S3 version; **no date component** — history is S3
versions only) and deletes the source, before tofu destroys the
infrastructure. An agent removed before it ever slept has no archive — only
the deterministically reproducible tofu-managed seed scaffold, which tofu
destroy removes; the archival step skips silently in that case.

## On pause/resume (sleep/wake)

Sleeping scales the ECS service to `desired_count=0`. The container receives
SIGTERM, runs `snapshot()`, and exits. S3 data remains intact. On wake
(`desired_count=1`), a new task boots and `restore()` extracts the workspace.

When the agent invokes the `sleep` tool, an automatic pre-sleep flush
runs first — capturing durable session knowledge into
`memory/YYYY-MM-DD.md` and refreshing the search index — before the SFN
fires and `snapshot()` runs. So the archived workspace always reflects the
agent's most recent durable-knowledge capture, even if the user never
asked the agent to write anything explicitly. See [memory.md](memory.md)
for the flush architecture.

## Excluded from the archive

| Pattern | Reason |
|---------|--------|
| `.cache/*` | Bytecode / pip / chromadb caches |
| `.aws/*` | Per-boot AWS SDK config (none today, reserved) |

`MEMORY_DATA_DIR` (`/var/lib/clawless-memory`: chroma + bm25 + graph) is a
separate ephemeral tree outside `$WORKSPACE_DIR` and is intentionally **not**
archived — the markdown under `memory/` is the source of truth; the index is
rebuilt on boot.

## Restore from S3

`scripts/restore-agent.sh` rolls a workspace back to a prior S3 version of
the single archive object. List the version history, then restore the newest
version older than a chosen cutoff:

```bash
# Inspect available versions (no service changes)
./scripts/restore-agent.sh --slug <client>/<agent> --list

# Roll back to the newest version before an ISO-8601 instant
./scripts/restore-agent.sh --slug <client>/<agent> --before 2026-04-10T12:00:00Z
```

The script scales the service to 0, copies the chosen version onto the
current key, then scales back to 1 with `--force-new-deployment` so the new
task extracts the rolled-back archive on boot.

To manually restore the current archive onto a running task (no rollback),
just force a new deployment — the new task's `restore()` re-extracts it:

```bash
aws ecs update-service --cluster clawless --service clawless-<client>-<agent> \
  --force-new-deployment --region us-east-1
```

To restore a removed agent's workspace (after re-adding the agent), copy the
single removed archive back onto the active key, then wake it:

```bash
aws s3 cp \
  s3://clawless-backups-<account>/removed/<client>/<agent>/workspace.tar.zst \
  s3://clawless-backups-<account>/agents/<client>/<agent>/workspace.tar.zst \
  --region us-east-1
./scripts/wake-agent.sh <client> <agent>
```

(Older removed states are noncurrent versions of that one key — list them
with `aws s3api list-object-versions --bucket clawless-backups-<account>
--prefix removed/<client>/<agent>/workspace.tar.zst`.)

## Cross-region replication (CRR)

All objects are replicated to `us-east-2` via S3 CRR. The replica uses
STANDARD_IA storage class. To restore from the replica (if the primary
region is unavailable):

```bash
aws s3 cp \
  s3://clawless-backups-replica-<account>/agents/<client>/<agent>/workspace.tar.zst \
  ./workspace.tar.zst --region us-east-2
```

## Monitoring

Check the container logs for snapshot/restore errors:

```bash
aws logs tail /clawless/fargate/<client>-<agent> --since 1h --region us-east-1 \
  | grep -iE 'snapshot|restore|archive'
```

A logged `snapshot ... (non-fatal)` line means the most recent session's
deltas were not persisted; the prior S3 version is still intact and the next
boot restores it.
