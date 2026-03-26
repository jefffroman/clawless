# Lifecycle Automation

All lifecycle operations are driven by a single Step Functions invocation that handles
the SSM write, records the event in DynamoDB, and invokes the Lifecycle Lambda.
Operators use the scripts in `scripts/` — never `tofu apply` directly for client ops.

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
GRAB → CLASSIFY → FAST PATH → SLOW PATH → RELEASE

1. GRAB: Scan table → conditional UpdateItem (in_progress=false → true) per slug.
         Only one Lambda can own a slug at a time.

2. CLASSIFY: Read SSM state for grabbed slugs.
   - Not in SSM → removal (slow path)
   - active=false → pause (slow path)
   - active=true  → resume/add (fast path)

3. FAST PATH: tofu apply for resumes/adds (immediate).
   - Post-apply: delete pause snapshots, deregister orphaned instances.

4. SLOW PATH: for each pause/removal:
   a. Stop instance (~5s — agent goes dark immediately)
   b. Start snapshot (pauses only, non-blocking)
   c. Poll snapshot — on each poll, check for intent changes:
      - Timestamp changed → re-read SSM → if now active: abandon snapshot,
        start_instance (no tofu apply needed — instance was never destroyed)
   d. After snapshot completes: tofu apply destroy
   e. After tofu: check intent again (can't interrupt mid-tofu):
      - If changed to active: run fast-path tofu apply to recreate

5. RELEASE: Conditional DeleteItem (timestamp must match grabbed value).
   - If condition fails → record was overwritten → unconditional delete.
```

**Key properties:**
- Resumes apply immediately — never blocked behind pause snapshots
- Agents go dark within ~5s of pause (stop_instance), not ~2 min (snapshot + destroy)
- Abandoned pauses (intent changed mid-snapshot) just restart the instance — no unnecessary tofu apply
- Removals skip snapshots — S3 workspace backups are the safety net
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

**Pause then quick resume (before Lambda grabs):**
SFN overwrites `pending` with the resume's timestamp. Lambda grabs → reads SSM → `active=true` → fast path (no-op if instance still running).

**Pause then resume (Lambda mid-snapshot):**
Lambda polls snapshot, calls `_intent_changed()` → `true` → reads SSM → `active=true` → abandons snapshot → `start_instance` → done. No tofu apply needed.

**Pause then resume (Lambda mid-tofu-destroy):**
Can't interrupt tofu. After destroy completes, Lambda checks `_intent_changed()` → `true` → reads SSM → `active=true` → runs fast-path tofu apply to recreate.

**Two Lambdas race on same slug:**
Both try conditional `UpdateItem(in_progress=false → true)`. DynamoDB serializes: one wins, other gets `ConditionalCheckFailedException` → skips.

**Lambda crashes mid-processing:**
Record stuck with `in_progress=true`. TTL (1 hour) expires → record auto-deleted. Next event creates a fresh record.

## Concurrency

DynamoDB conditional `UpdateItem` is atomic per item — only one Lambda invocation can own each slug. If two Lambdas race, one gets the lock and the other skips.

For tofu state lock contention (rare — only when an operator runs a local `tofu apply`), the handler retries 15 times at 3-second intervals (45s max). On exhaustion, it sends an SNS alert and exits. The unprocessed work appears as drift on the next invocation.

## Local vs. Lambda applies

**The Lambda handles**: agent-level resources — everything inside `module.client["slug"]`. This includes the Lightsail instance, IAM role, SSM activation, and firewall rules.

**Local `tofu apply` handles**: root-level infrastructure — the Lambda itself, ECR repo, Step Functions workflow, DynamoDB table, SNS topic, CloudWatch alarms, and budget alerts. These resources are not targeted by the Lambda.

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

Then pause and resume the agent (or use any script that invokes SFN).
