# Lifecycle Automation

All lifecycle operations are driven by a single Step Functions invocation that handles
the SSM write, records the event in DynamoDB, and invokes the Lifecycle Lambda.
Operators use the scripts in `scripts/` — never `tofu apply` directly for client ops.

## Resource classification

| Resource | Type | Survives sleep? | Survives remove? |
|---|---|---|---|
| ECS service + task definition | Durable | Yes (desired=0) | No |
| IAM task role + policies | Durable | Yes | No |
| S3 workspace prefix | Durable | Yes | No (archived first) |
| CloudWatch log group | Durable | Yes | No |
| Shared S3 backup bucket | Durable (shared) | Yes | Yes |
| CRR replication config + role | Durable (shared) | Yes | Yes |

There are no per-agent S3 buckets. All agents share `clawless-backups-{account}` with per-agent prefixes.

## Agent states

### Active (`active: true` in SSM, or default)

All resources exist. ECS service running at `desired_count=1`. Gateway container syncs workspace from S3 on boot.

```bash
./scripts/add-agent.sh
```

### Sleeping (`active: false` in SSM)

ECS service at `desired_count=0`. No running tasks — no compute costs. All durable resources (IAM, S3 data, log group, task def) preserved. Container ran `sync_up()` on SIGTERM before exiting.

**Telegram only:** On sleep, the gateway container redirects the agent's Telegram webhook to the wake-listener Lambda Function URL (`setWebhook`). While sleeping, incoming Telegram messages are queued in DynamoDB (`clawless-wake-messages`) and trigger an automatic wake via the lifecycle SFN.

On wake, the gateway deletes the webhook (`deleteWebhook`) immediately before reading queued messages from DynamoDB, ensuring no messages are lost during the handoff.

```bash
./scripts/sleep-agent.sh <client-slug>-<agent-slug>
./scripts/wake-agent.sh <client-slug>-<agent-slug>
```

> **Gateway token** persists in SSM SecureString — no reconfiguration needed on wake.

### Removed (SSM key deleted)

All resources destroyed. Workspace archived to `removed/{slug}/{date}/` in the shared bucket before teardown.

```bash
./scripts/remove-agent.sh <client-slug> <agent-slug>
```

## Event flow

All agent operations (add, remove, sleep, wake) follow the same path:

```
Script / UI → Step Functions Express Workflow:
               1. WriteSSM (PutParameter or DeleteParameter)
               2. WritePending (DynamoDB UpdateItem — one record per slug, last-write-wins)
               3. CheckInProgress → Lambda invoke (async, only if no Lambda already owns the slug)
```

1. A script (or the UI) constructs the desired SSM value and invokes the Step Functions workflow
2. SFN writes the agent config to SSM (SecureString) — or deletes it for removals
3. SFN writes a pending record to DynamoDB (`clawless-lifecycle-pending`) with the operation and timestamp
4. If no Lambda currently owns the slug (`in_progress = false`), SFN invokes the Lambda asynchronously
5. If a Lambda already owns the slug, the new event is recorded in DynamoDB (last-write-wins) and the owning Lambda detects the intent change during processing

Scripts and the UI only need **one API call** (`start-execution`) — SFN handles both the SSM write and lifecycle coordination.

### Channel-triggered wake

When a sleeping Telegram agent receives a message, it follows a different entry point:

```
Telegram → Wake Listener Lambda (Function URL):
             1. Queue message in DynamoDB (clawless-wake-messages, list append)
             2. Set SSM /active=true
             3. Start lifecycle SFN execution
             4. Reply to user: "waking up…"
```

The lifecycle Lambda then picks up the wake via the normal GRAB → CLASSIFY → FAST PATH flow. On boot, the gateway entrypoint deletes the Telegram webhook (restoring long-polling) and replays queued messages from DynamoDB.

### SFN states

| State | Type | Purpose |
|---|---|---|
| ExtractSlug | Pass | Parse `client/agent` slug from SSM path |
| ChooseSSMAction | Choice | Delete → DeleteSSM, else → WriteSSM |
| WriteSSM | Task | `ssm:PutParameter` (SecureString, overwrite) |
| DeleteSSM | Task | `ssm:DeleteParameter` |
| WritePending | Task | DynamoDB `UpdateItem` — sets `pending`, `timestamp`, preserves `in_progress` via `if_not_exists` |
| CheckInProgress | Choice | `in_progress = true` → AlreadyOwned (succeed), else → InvokeLambda |
| InvokeLambda | Task | Async Lambda invocation |

## What the Lambda does

The Lambda uses a **single-table DynamoDB design** with per-slug ownership:

```
GRAB → CLASSIFY → FAST/SLOW DISPATCH → RELEASE

1. GRAB: Scan table → conditional UpdateItem (in_progress=false → true) per slug.
         Only one Lambda can own a slug at a time.

2. CLASSIFY: Read SSM state for grabbed slugs.
   - Not in SSM → removal (slow path — tofu destroy)
   - active=false → sleep (fast path — ECS desired=0)
   - active=true  → wake/add

3. FAST PATH (sleep/wake):
   - Existing service → ecs:UpdateService desired=0 or 1. Done in seconds.
   - Wake is a single UpdateService call: `desiredCount=1` is combined with `forceNewDeployment=True` only when :latest has been pushed to ECR since the last deployment. One API call → one deployment → one task launch, on the fresh image. ECS otherwise caches the resolved digest, so omitting forceNewDeployment on an unchanged image keeps wake speed predictable.
   - Container SIGTERM handler syncs workspace to S3 on sleep.
   - Container sync_down restores workspace on wake.

4. SLOW PATH (add/remove):
   - Add: tofu apply creates ECS service, task def, IAM role, seed S3 workspace.
   - Remove: archive S3 prefix, tofu destroy the module.

5. RELEASE: Conditional DeleteItem (timestamp must match grabbed value).
   - If condition fails → record was overwritten → unconditional delete.
```

**Key properties:**
- Sleep/wake are pure ECS API calls — no tofu apply, completes in seconds
- Adds and removes go through tofu apply (clones repo at `/clawless/version`)
- New events arriving during processing are detected via timestamp comparison

## Per-slug ownership and race handling

The DynamoDB table (`clawless-lifecycle-pending`) has one record per slug:

| Field | Type | Writer | Purpose |
|---|---|---|---|
| `slug` | S (hash key) | SFN | `client/agent` path |
| `pending` | S | SFN only | SSM operation: `"Create"`, `"Update"`, `"Delete"` |
| `in_progress` | BOOL | Lambda only | Ownership lock |
| `timestamp` | S | SFN | Event time — used to detect intent changes |
| `ttl` | N | Lambda | 1-hour safety net for orphaned records |

**Invariants:**
- SFN never touches `in_progress`. Lambda never touches `pending`.
- One record per slug. Last SFN write wins.
- `in_progress = true` means a Lambda owns the slug. Other Lambdas skip it.

### Race scenarios

**Sleep then quick wake (before Lambda grabs):**
SFN overwrites `pending` with the wake's timestamp. Lambda grabs → reads SSM → `active=true` → `ecs:UpdateService desired=1`.

**Two Lambdas race on same slug:**
Both try conditional `UpdateItem(in_progress=false → true)`. DynamoDB serializes: one wins, other gets `ConditionalCheckFailedException` → skips.

**Lambda crashes mid-processing:**
Record stuck with `in_progress=true`. TTL (1 hour) expires → record auto-deleted. Next event creates a fresh record.

## Concurrency

DynamoDB conditional `UpdateItem` is atomic per item — only one Lambda invocation can own each slug. If two Lambdas race, one gets the lock and the other skips.

For tofu state lock contention (rare — only when an operator runs a local `tofu apply`), the handler retries 15 times at 3-second intervals (45s max). On exhaustion, it sends an SNS alert and exits. The unprocessed work appears as drift on the next invocation.

## Local vs. Lambda applies

**The Lambda handles**: agent-level resources — everything inside `module.client["slug"]`. This includes the ECS service, task definition, task role, IAM policies, log group, and S3 seed objects.

**Local `tofu apply` handles**: root-level infrastructure — the Lambda itself, ECR repos, Step Functions workflow, DynamoDB tables, SNS topic, VPC, ECS cluster, SearXNG Lambda, wake-listener Lambda, and budget alerts. These resources are not targeted by the Lambda.

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

To retry a failed agent, delete its error flag and trigger a lifecycle event:

```bash
aws ssm delete-parameter --name "/clawless/clients/<client>/<agent>/error" --region us-east-1
```

Then wake the agent (or use any script that invokes SFN).
