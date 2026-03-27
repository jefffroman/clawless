# Backups & Restore

## Overview

Every active agent syncs its OpenClaw workspace to S3 hourly. All agents share a single versioned bucket with cross-region replication. When an agent is removed, its workspace is archived before teardown.

| Component | Detail |
|-----------|--------|
| Source | `~/.openclaw/workspace/` on each instance |
| Bucket | `clawless-backups-{account}` (us-east-1) |
| Prefix | `agents/{client}/{agent}/workspace/` |
| Replica | `clawless-backups-replica-{account}` (us-east-2, STANDARD_IA) |
| Schedule | Hourly via systemd timer (`clawless-backup.timer`) |
| Versioning | Enabled — current kept indefinitely, 2 noncurrent versions kept 7 days |
| Encryption | AES-256 (SSE-S3) |

## How it works

The `backup` Ansible role deploys two pieces:

1. **`clawless-backup.service`** — a oneshot systemd unit that runs `/usr/local/bin/clawless-backup`
2. **`clawless-backup.timer`** — fires hourly, persistent (catches up after downtime)

The backup script runs `aws s3 sync --delete` and reports a CloudWatch metric (`Clawless/Backup/BackupFailure`) — `0` on success, `1` on failure.

## Retention

| Layer | Retention |
|-------|-----------|
| Current version | Indefinite |
| Noncurrent versions | 2 most recent kept, deleted after 7 days |
| Expired delete markers | Auto-cleaned |
| Replica (us-east-2) | Same versioning; inherits lifecycle from source |

## On removal

When an agent is removed, the Lambda copies all objects from the agent's backup prefix to `removed/{slug}/{date}/` in the same bucket before destroying infrastructure. This ensures workspace data survives even after all agent resources are torn down.

## On pause/resume

Pausing does **not** touch backups. The instance is snapshotted and destroyed, but S3 data remains intact. On resume, the instance is recreated from the snapshot — the backup timer resumes automatically.

## Restore from S3

To restore a workspace from backup onto a running instance:

```bash
# Restore the latest workspace backup
./scripts/ssm-run.sh --slug <client>-<agent> \
  "aws s3 sync s3://clawless-backups-\$(aws sts get-caller-identity --query Account --output text)/agents/<client>/<agent>/workspace/ /home/ubuntu/.openclaw/workspace/ && sudo -u ubuntu XDG_RUNTIME_DIR=/run/user/\$(id -u ubuntu) systemctl --user restart openclaw-gateway"
```

To restore a removed agent's workspace (after re-adding the agent):

```bash
# List available removal archives
aws s3 ls s3://clawless-backups-<account>/removed/<client>-<agent>/ --region us-east-1

# Restore from a specific date
./scripts/ssm-run.sh --slug <client>-<agent> \
  "aws s3 sync s3://clawless-backups-<account>/removed/<client>-<agent>/<date>/ /home/ubuntu/.openclaw/workspace/"
```

## Cross-region replication (CRR)

All objects are replicated to `us-east-2` via S3 CRR. The replica uses STANDARD_IA storage class. To restore from the replica (if the primary region is unavailable):

```bash
aws s3 sync s3://clawless-backups-replica-<account>/agents/<client>/<agent>/workspace/ ./local-restore/ --region us-east-2
```

## Monitoring

CloudWatch alarm `BackupFailure` fires when any agent's backup reports a non-zero metric. Check the agent's backup timer and credentials:

```bash
./scripts/ssm-run.sh --slug <client>-<agent> "systemctl status clawless-backup.timer && journalctl -u clawless-backup --since '1 hour ago' --no-pager"
```
