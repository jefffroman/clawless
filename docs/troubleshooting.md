# Troubleshooting

## Task status

**Check if the ECS service is running:**
```bash
aws ecs describe-services --cluster clawless --services clawless-<client>-<agent> \
  --query 'services[0].{status:status,desired:desiredCount,running:runningCount}' \
  --region us-east-1
```

**View recent task events (scheduling failures, OOM, etc.):**
```bash
aws ecs describe-services --cluster clawless --services clawless-<client>-<agent> \
  --query 'services[0].events[:5]' --region us-east-1
```

## Container logs

Gateway logs go to CloudWatch at `/clawless/fargate/<client>-<agent>`:

```bash
aws logs tail /clawless/fargate/<client>-<agent> --since 1h --region us-east-1
```

**Follow logs in real time:**
```bash
aws logs tail /clawless/fargate/<client>-<agent> --follow --region us-east-1
```

**Enable verbose (DEBUG-level) gateway logs without redeploying:**
```bash
aws ssm put-parameter \
  --name "/clawless/clients/<client>/<agent>/verbose" \
  --type String --value true --overwrite --region us-east-1
./scripts/sleep-agent.sh <client> <agent>   # entrypoint reads /verbose at boot
./scripts/wake-agent.sh  <client> <agent>
```

## Broken transcripts

If the gateway crashes mid-turn or you see Bedrock errors about message ordering ("toolUse without matching toolResult", etc.), strip the agent's transcripts so the next boot starts fresh. The workspace is a single archive object now, so this is a download → extract → edit → repack → upload cycle. Do it while the agent is asleep (scale to 0 first) so the running task can't snapshot over your edit:

```bash
ACCT=<account>; SLUG=<client>/<agent>; SVC=clawless-<client>-<agent>
KEY="agents/${SLUG}/workspace.tar.zst"

aws ecs update-service --cluster clawless --service "$SVC" \
  --desired-count 0 --region us-east-1 >/dev/null
aws ecs wait services-stable --cluster clawless --services "$SVC" --region us-east-1

WORK=$(mktemp -d)
aws s3 cp "s3://clawless-backups-${ACCT}/${KEY}" - --region us-east-1 \
  | zstd -dc | tar -C "$WORK" -xf -
rm -rf "$WORK"/transcripts/*          # memory/ is left untouched
tar -C "$WORK" --exclude='./.cache' --exclude='./.aws' -cf - . \
  | zstd -1 -T0 | aws s3 cp - "s3://clawless-backups-${ACCT}/${KEY}" --region us-east-1
rm -rf "$WORK"
```

Then wake the agent (or scale back to 1) — the new task extracts the cleaned archive on boot:

```bash
./scripts/wake-agent.sh <client> <agent>
```

Only the per-peer session JSONLs under `transcripts/` are removed; the agent's `memory/` files ride along untouched. If you'd rather roll the whole workspace back to a known-good point instead of surgically editing, use `./scripts/restore-agent.sh --slug <client>/<agent> --list` to pick a prior archive version.

## Memory / flush / compaction

See [memory.md](memory.md) for the full architecture. Common operational
checks:

**Confirm a session's flush state** (the file rides inside the workspace archive; extract just that member to stdout):
```bash
aws s3 cp s3://clawless-backups-<account>/agents/<client>/<agent>/workspace.tar.zst - --region us-east-1 \
  | zstd -dc | tar -xO ./memory/.flush_state.json
```
Each entry is `"<sid>": "<iso-ts>"` — the high-water mark of the most
recent successful flush. Missing entries mean the session has never been
flushed (which is normal for fresh sessions and for first-deployment
sessions, where existing transcripts are bootstrapped to "already
flushed" to avoid a massive one-time backfill).

**Watch flush activity live:**
```bash
aws logs tail /clawless/fargate/<client>-<agent> --follow --region us-east-1 \
  | grep -E "flush|reindex|compaction"
```

**Reset a stuck index** (rare — only if `chroma_db` is corrupted or the
`sync_state.json` is out of sync with the workspace markdown):
```bash
aws ecs update-service --cluster clawless --service clawless-<client>-<agent> \
  --force-new-deployment --region us-east-1
```
The new task rebuilds the index from scratch on boot.

**Disable periodic flush** (e.g., to control cost during a long passive
session): set `CLAWLESS_PERIODIC_GROWTH_THRESHOLD` to a very high value
(e.g., `999999999`) on the task definition env, then force-new-deployment.

## Lifecycle Lambda

**Check recent invocations:**
```bash
aws logs tail /aws/lambda/clawless-lifecycle --since 1h --region us-east-1
```

**Check for error flags blocking an agent:**
```bash
aws ssm get-parameter --name "/clawless/clients/<client>/<agent>/error" --region us-east-1
```

**Clear error flag and retry:**
```bash
aws ssm delete-parameter --name "/clawless/clients/<client>/<agent>/error" --region us-east-1
./scripts/wake-agent.sh <client>-<agent>
```

## Credentials

The gateway's boto3 picks up the task role automatically via the ECS metadata endpoint (`AWS_CONTAINER_CREDENTIALS_RELATIVE_URI`). If you see `AccessDeniedException` in the logs, the task role's IAM policy is missing the action — see `tofu/modules/client/main.tf` for the per-agent grants.

The agent's `bash` tool runs as a separate UID (`clawless-tool`) with the AWS credential env vars stripped, so it cannot inherit task-role auth. AWS-bound work (sleep, web_search) happens in-process via the gateway's own boto3 clients.

**Force a fresh task (picks up any IAM policy changes):**
```bash
aws ecs update-service --cluster clawless --service clawless-<client>-<agent> \
  --force-new-deployment --region us-east-1
```
