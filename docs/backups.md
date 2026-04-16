# Backups & Restore

## Overview

Every active agent syncs its OpenClaw workspace to S3 on boot (sync-down) and shutdown (sync-up via SIGTERM handler). All agents share a single versioned bucket with cross-region replication. When an agent is removed, its workspace is archived before teardown.

| Component | Detail |
|-----------|--------|
| Source | `$HOME/.openclaw/workspace/` in each gateway container |
| Bucket | `clawless-backups-{account}` (us-east-1) |
| Prefix | `agents/{client}/{agent}/workspace/` |
| Replica | `clawless-backups-replica-{account}` (us-east-2, STANDARD_IA) |
| Sync trigger | Container boot (down) and SIGTERM (up) — see `entrypoint.sh` |
| Versioning | Enabled — current kept indefinitely, 2 noncurrent versions kept 7 days |
| Encryption | AES-256 (SSE-S3) |

## How it works

The gateway container's `entrypoint.sh` handles sync in two places:

1. **`sync_down()`** — runs at boot, pulls the workspace from S3 (excludes `openclaw.json` and `.aws/`)
2. **`sync_up()`** — runs on SIGTERM (sleep or task stop), pushes the workspace back to S3 (excludes session locks, `openclaw.json`, `.aws/`, `__pycache__`)

There is no periodic timer — sync happens at lifecycle boundaries (boot and shutdown). The Fargate task's SIGTERM handler (`shutdown()`) calls `sync_up` before exiting.

## Retention

| Layer | Retention |
|-------|-----------|
| Current version | Indefinite |
| Noncurrent versions | 2 most recent kept, deleted after 7 days |
| Expired delete markers | Auto-cleaned |
| Replica (us-east-2) | Same versioning; inherits lifecycle from source |

## On removal

When an agent is removed, the Lambda copies all objects from the agent's backup prefix to `removed/{slug}/{date}/` in the same bucket before destroying infrastructure. This ensures workspace data survives even after all agent resources are torn down.

## On pause/resume (sleep/wake)

Sleeping scales the ECS service to `desired_count=0`. The container receives SIGTERM, runs `sync_up()`, and exits. S3 data remains intact. On wake (`desired_count=1`), a new task boots and `sync_down()` restores the workspace.

## Excluded from sync

| Pattern | Reason |
|---------|--------|
| `.openclaw/openclaw.json` | Rebuilt from baseline + `configure-openclaw` at each boot |
| `.openclaw/openclaw.json.bak*` | Stale backups of the above |
| `.openclaw/agents/*/sessions/*.lock` | Per-process session locks |
| `.aws/*` | Per-boot credential_process helper |
| `vector_memory/__pycache__/` | Bytecode cache |

## Restore from S3

Use `scripts/restore-agent.sh` to roll a workspace back to a prior S3 version window:

```bash
./scripts/restore-agent.sh <client>-<agent>
```

To manually restore the latest backup onto a running task, force a new deployment (the new task will `sync_down` the current S3 state):

```bash
aws ecs update-service --cluster clawless --service clawless-<client>-<agent> \
  --force-new-deployment --region us-east-1
```

To restore a removed agent's workspace (after re-adding the agent):

```bash
# List available removal archives
aws s3 ls s3://clawless-backups-<account>/removed/<client>-<agent>/ --region us-east-1

# Copy archive back to the active prefix, then wake the agent
aws s3 sync s3://clawless-backups-<account>/removed/<client>-<agent>/<date>/ \
  s3://clawless-backups-<account>/agents/<client>/<agent>/workspace/ --region us-east-1
./scripts/wake-agent.sh <client>-<agent>
```

## Cross-region replication (CRR)

All objects are replicated to `us-east-2` via S3 CRR. The replica uses STANDARD_IA storage class. To restore from the replica (if the primary region is unavailable):

```bash
aws s3 sync s3://clawless-backups-replica-<account>/agents/<client>/<agent>/workspace/ ./local-restore/ --region us-east-2
```

## Monitoring

CloudWatch alarm `BackupFailure` fires when any agent's backup reports a non-zero metric. Check the container logs for sync errors:

```bash
aws logs tail /ecs/clawless-<client>-<agent> --since 1h --region us-east-1 | grep -i sync
```
