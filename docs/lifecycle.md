# Lifecycle Automation

All lifecycle operations are driven by changes to the SSM Parameter Store hierarchy
(`/clawless/clients/{client-slug}/{agent-slug}`). Operators use the scripts in `scripts/` — never `tofu apply` directly for client ops.

## Resource classification

| Resource | Type | Survives pause? | Survives remove? |
|---|---|---|---|
| Lightsail instance | Ephemeral | No (snapshotted) | No |
| SSM activation | Ephemeral | No | No |
| Lightsail firewall rules | Ephemeral | No | No |
| IAM role + policies | Durable | Yes | No |
| Shared S3 backup bucket | Durable (shared) | Yes | No (workspace archived first) |
| CRR replication config + role | Durable (shared) | Yes | Yes |

There are no per-agent S3 buckets. All agents share `clawless-backups-{account}` with per-agent prefixes.

## Agent states

### Active (`active: true` in SSM, or default)

All resources exist. Instance is running OpenClaw. Hourly backup timer syncs the workspace to the shared backup bucket.

```bash
./scripts/add-agent.sh
```

### Paused (`active: false` in SSM)

Ephemeral resources destroyed. Durable resources (IAM, S3 data) preserved. No compute costs. Snapshot billed at ~$0.05/GB of actual used disk.

```bash
./scripts/pause-agent.sh <client-slug> <agent-slug>
./scripts/resume-agent.sh <client-slug> <agent-slug>
```

> **Gateway token** persists in the snapshot (`/etc/openclaw/openclaw.env`) — no reconfiguration needed on resume.

> **Cannot restore to a smaller Lightsail plan** than the one the snapshot was created on.

### Removed (SSM key deleted)

All resources destroyed. Workspace archived to `removed/{slug}/{date}/` in the shared bucket before teardown.

```bash
./scripts/remove-agent.sh <client-slug> <agent-slug>
```

## Event flow

All agent operations (add, remove, pause, resume) follow the same path:

```
SSM parameter change → EventBridge → Step Functions Express Workflow:
                                       1. DynamoDB PutItem (event record)
                                       2. Lambda invoke (async)
```

1. An operator (or the storefront) writes/deletes an SSM parameter under `/clawless/clients/`
2. EventBridge matches the change (wildcard: `/clawless/clients/*/*`)
3. A Step Functions Express Workflow writes the event to a DynamoDB table, then invokes the lifecycle Lambda asynchronously
4. The Lambda atomically grabs events from DynamoDB and processes them

The EventBridge rule only matches agent-level parameters (5-segment paths). Step Functions guarantees the event is durably written before the Lambda starts — if the Lambda fails or is already running, the event persists in DynamoDB for the next invocation.

## What the Lambda does

The Lambda runs a **resume-first processing loop** that prioritises time-critical operations:

```
PENDING_DESTROYS = []

loop:
  1. Grab events from DynamoDB (atomic DeleteItem per record)
  2. Read SSM state for affected slugs
  3. Classify: resumes/adds (fast path) vs pauses/removals (slow path)
  4. For each pause/removal: stop instance (~5s — agent goes dark immediately)
  5. For each pause: start snapshot (non-blocking)
  6. Accumulate pauses/removals in PENDING_DESTROYS
  7. Run tofu apply for resumes/adds
  8. Wait for snapshots to complete
  9. Grab more events → loop if any

after loop:
  10. Run tofu apply for all PENDING_DESTROYS
  11. Post-apply cleanup
```

**Key properties:**
- Resumes apply immediately each iteration — never blocked behind pause snapshots
- Agents go dark within ~5s of pause (stop_instance), not ~2 min (snapshot + destroy)
- Removals skip snapshots — S3 workspace backups are the safety net
- Snapshots run in background while resume tofu applies execute
- New events arriving during processing get picked up at step 9

## Concurrency

DynamoDB `DeleteItem` with `ReturnValues=ALL_OLD` is atomic per item — only one Lambda invocation gets each event record. If two Lambdas race, one gets the event and the other gets nothing (exits or processes other events).

For tofu state lock contention (rare — only when an operator runs a local `tofu apply`), the handler retries 15 times at 3-second intervals (45s max). On exhaustion, it sends an SNS alert and exits. Events are already consumed; the unprocessed work appears as drift on the next invocation.

## Local vs. Lambda applies

**The Lambda handles**: agent-level resources — everything inside `module.client["slug"]`. This includes the Lightsail instance, IAM role, SSM activation, and firewall rules.

**Local `tofu apply` handles**: root-level infrastructure — the Lambda itself, ECR repo, EventBridge rules, Step Functions workflow, DynamoDB table, SNS topic, CloudWatch alarms, and budget alerts. These resources are not targeted by the Lambda.

After changing `lambda/handler.py`, `lambda/Dockerfile`, or any root-level tofu config:

```bash
cd tofu && tofu apply
```

The Lambda image is rebuilt automatically when `tofu apply` detects changes to `handler.py` or `Dockerfile` (via `null_resource.lambda_image`).

## Error handling

When `tofu apply` fails for an agent:

1. The failed resources are tainted (so the next apply recreates them)
2. An `/error` SSM parameter is written at `/clawless/clients/{client}/{agent}/error`
3. An SNS alert is sent to the alerts topic
4. The agent is skipped on subsequent Lambda runs until the `/error` param is manually deleted

To retry a failed agent, delete its error flag:

```bash
aws ssm delete-parameter --name "/clawless/clients/<client>/<agent>/error" --region us-east-1
./scripts/trigger-lifecycle.sh
```

## EventBridge DLQ

If an EventBridge event exhausts all 185 retries (over 24 hours), it lands in the `clawless-eventbridge-dlq` SQS queue. A CloudWatch alarm fires when any message appears in this queue, alerting the operator to investigate.
