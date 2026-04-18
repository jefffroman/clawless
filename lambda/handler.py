"""
Clawless lifecycle Lambda (Fargate).

Triggered by Step Functions on lifecycle changes. Scripts and the UI invoke SFN
directly; SFN handles the SSM write, writes a pending record to DynamoDB (one per
slug, last-write-wins), and invokes the Lambda only if no other Lambda already owns
that slug.

The Lambda atomically grabs unowned records by setting in_progress=true (conditional
on false). One Lambda owns each slug until done — no cross-Lambda coordination.

Lifecycle transitions:
  - Add agent:    seed S3 workspace, tofu apply (creates ECS service desired=1)
  - Remove agent: archive S3 prefix, tofu destroy the ECS service
  - Pause agent:  ecs:UpdateService desired=0 (SIGTERM → container sync-up)
  - Resume agent: ecs:UpdateService desired=1 (container boots, sync-down)

Pause/resume are pure ECS API calls — no tofu apply involved — so they're
fast and cheap. Only adds and removes touch tofu.

Failure handling:
  - State lock contention: retry 15 times at 3s intervals (45s max).
  - Single-slug failure: taint partial state, write /error to SSM, exclude slug,
    retry remaining slugs. SNS alert for every failure.
  - Mass failure (>1 slug or systemic): stop, alert, let operator investigate.
"""

import datetime
import json
import os
import re
import shutil
import subprocess
import tempfile
import time

import boto3
from botocore.exceptions import ClientError

ssm = boto3.client("ssm")
s3 = boto3.client("s3")
dynamodb = boto3.client("dynamodb")
sns = boto3.client("sns")
ecs = boto3.client("ecs")

STATE_BUCKET = os.environ["STATE_BUCKET"]
REPO_URL = os.environ["REPO_URL"]
REGION = os.environ["AWS_DEFAULT_REGION"]
LIFECYCLE_TABLE = os.environ.get("LIFECYCLE_TABLE", "")
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")
ECS_CLUSTER = os.environ.get("ECS_CLUSTER", "clawless")
BACKUP_BUCKET = os.environ["BACKUP_BUCKET"]
PLUGIN_CACHE_DIR = "/opt/tofu-plugin-cache"

LOCK_RETRY_INTERVAL = 3   # seconds between retries when state lock is held
LOCK_MAX_RETRIES = 15     # 45s total — covers brief contention from local applies


class StateLockError(Exception):
    """Raised when tofu apply fails due to state lock contention."""
    pass


# ── Entry point ──────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    print(f"Event: {json.dumps(event)}")

    version = ssm.get_parameter(Name="/clawless/version")["Parameter"]["Value"]
    print(f"Clawless version: {version}")

    # ── Step 1: GRAB — atomically claim unowned records ────────────────
    grabbed = _grab_all()
    if not grabbed:
        print("Nothing to grab — done")
        return {"status": "noop"}

    print(f"Grabbed {len(grabbed)} slug(s): {sorted(grabbed.keys())}")

    # ── Step 2: CLASSIFY — read SSM to determine intent ────────────────
    agents = _get_agents()
    errored_slugs = {slug for slug, cfg in agents.items() if cfg.get("_error")}
    if errored_slugs:
        print(f"Skipping agents with /error state: {sorted(errored_slugs)}")

    fast_slugs = set()   # resumes + adds — apply immediately
    slow_slugs = set()   # pauses + removals — stop now, destroy later

    for slug in grabbed:
        if slug in errored_slugs:
            continue
        if slug not in agents:
            slow_slugs.add(slug)
        elif not agents[slug].get("active", True):
            slow_slugs.add(slug)
        else:
            fast_slugs.add(slug)

    # ── Step 2b: PAUSE/RESUME DISPATCH — ECS API only, no tofu ────────
    # For existing services: resume is desired=1, pause is desired=0.
    # Container SIGTERM handler runs sync-up on stop; sync-down on next boot.
    for slug in list(fast_slugs):
        if _fargate_service_exists(slug):
            _fargate_wake(slug)
            fast_slugs.discard(slug)

    for slug in list(slow_slugs):
        if slug in agents and _fargate_service_exists(slug):
            # Pure pause: scale to 0 and we're done (no tofu destroy).
            print(f"[fargate:{slug}] pause via ecs:UpdateService desired=0")
            _fargate_set_desired(slug, 0)
            slow_slugs.discard(slug)
        elif slug not in agents and _fargate_service_exists(slug):
            # Removal: stop the task now so it doesn't process events while
            # we archive and tofu destroys. Keep in slow_slugs for tofu.
            print(f"[fargate:{slug}] remove — scaling to 0 before tofu destroy")
            _fargate_set_desired(slug, 0)

    # ── Step 3: FAST PATH — tofu apply for new adds ───────────────────
    # Workspace seeding is handled inside the client module via aws_s3_object
    # resources — tofu applies the seeds before the ECS service is created.
    if fast_slugs:
        print(f"Fast path (new adds): {sorted(fast_slugs)}")
        _apply_with_retry(version, agents, fast_slugs, errored_slugs)

    # ── Step 4: SLOW PATH — tofu destroy for removals ─────────────────
    if slow_slugs:
        print(f"Slow path (removals): {sorted(slow_slugs)}")
        agents = _get_agents()
        errored_slugs = {slug for slug, cfg in agents.items() if cfg.get("_error")}
        _apply_with_retry(version, agents, slow_slugs, errored_slugs)

    # ── Step 5: RELEASE — conditional delete for each owned slug ───────
    for slug, info in grabbed.items():
        _release(slug, info["timestamp"])

    return {"status": "success"}


# ── DynamoDB lifecycle table ─────────────────────────────────────────────────

def _grab_all():
    """Atomically grab all unowned records from the lifecycle table.

    Scans the table, then does a conditional UpdateItem on each record to set
    in_progress=true (only if currently false). Returns {slug: {timestamp, pending}}
    for successfully grabbed records. Sets TTL on grab as a crash safety net.
    """
    if not LIFECYCLE_TABLE:
        print("No LIFECYCLE_TABLE configured — manual invocation")
        return {}

    items = []
    response = dynamodb.scan(TableName=LIFECYCLE_TABLE)
    items.extend(response.get("Items", []))
    while response.get("LastEvaluatedKey"):
        response = dynamodb.scan(
            TableName=LIFECYCLE_TABLE,
            ExclusiveStartKey=response["LastEvaluatedKey"],
        )
        items.extend(response.get("Items", []))

    if not items:
        return {}

    grabbed = {}
    ttl = int(time.time()) + 3600  # 1 hour safety net

    for item in items:
        slug = item["slug"]["S"]
        # Skip records already owned by another Lambda
        if item.get("in_progress", {}).get("BOOL", False):
            print(f"[grab] {slug} already in_progress — skipping")
            continue

        try:
            resp = dynamodb.update_item(
                TableName=LIFECYCLE_TABLE,
                Key={"slug": {"S": slug}},
                UpdateExpression="SET in_progress = :true, #ttl = :ttl",
                ExpressionAttributeNames={"#ttl": "ttl"},
                ExpressionAttributeValues={
                    ":true": {"BOOL": True},
                    ":false": {"BOOL": False},
                    ":ttl": {"N": str(ttl)},
                },
                ConditionExpression="in_progress = :false",
                ReturnValues="ALL_NEW",
            )
            attrs = resp.get("Attributes", {})
            grabbed[slug] = {
                "timestamp": attrs.get("timestamp", {}).get("S", ""),
                "pending": attrs.get("pending", {}).get("S", ""),
            }
            print(f"[grab] {slug} — grabbed (pending={grabbed[slug]['pending']})")
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                print(f"[grab] {slug} — lost race, another Lambda owns it")
                continue
            raise

    return grabbed


def _intent_changed(slug, grabbed_timestamp):
    """Check if a new event arrived for this slug since we grabbed it.

    Does a ConsistentRead GetItem and compares timestamp to the one we grabbed.
    Returns True if timestamp changed (new event arrived).
    """
    if not LIFECYCLE_TABLE:
        return False
    try:
        resp = dynamodb.get_item(
            TableName=LIFECYCLE_TABLE,
            Key={"slug": {"S": slug}},
            ConsistentRead=True,
        )
        item = resp.get("Item")
        if not item:
            return False  # record was deleted (shouldn't happen while we own it)
        current_ts = item.get("timestamp", {}).get("S", "")
        return current_ts != grabbed_timestamp
    except ClientError:
        return False  # on error, assume no change (safe default)


def _release(slug, grabbed_timestamp):
    """Release ownership of a slug. Conditional delete — only if timestamp matches.

    If timestamp changed (new event arrived during processing), re-process once
    then delete unconditionally.
    """
    if not LIFECYCLE_TABLE:
        return

    try:
        dynamodb.delete_item(
            TableName=LIFECYCLE_TABLE,
            Key={"slug": {"S": slug}},
            ConditionExpression="#ts = :ts",
            ExpressionAttributeNames={"#ts": "timestamp"},
            ExpressionAttributeValues={":ts": {"S": grabbed_timestamp}},
        )
        print(f"[release:{slug}] deleted (timestamp matched)")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            # Timestamp changed — a new event arrived while we were processing.
            # SFN already tried to invoke a Lambda for this, but it was skipped
            # because in_progress=true. We need to handle this one more pass.
            print(f"[release:{slug}] timestamp changed — new event during processing")
            # Delete unconditionally — we're the owner, we'll handle it
            dynamodb.delete_item(
                TableName=LIFECYCLE_TABLE,
                Key={"slug": {"S": slug}},
            )
            print(f"[release:{slug}] deleted unconditionally (will be re-triggered by next event)")
        else:
            raise


# ── Fargate helpers ──────────────────────────────────────────────────────────

def _fargate_service_name(slug):
    """Agent slug 'client/agent' → ECS service name 'clawless-client-agent'."""
    return "clawless-" + slug.replace("/", "-")


def _fargate_service_exists(slug):
    name = _fargate_service_name(slug)
    try:
        resp = ecs.describe_services(cluster=ECS_CLUSTER, services=[name])
    except ClientError as e:
        print(f"[fargate:{slug}] describe_services failed: {e}")
        return False
    services = [s for s in resp.get("services", []) if s.get("status") != "INACTIVE"]
    return bool(services)


def _fargate_set_desired(slug, count):
    name = _fargate_service_name(slug)
    ecs.update_service(cluster=ECS_CLUSTER, service=name, desiredCount=count)
    print(f"[fargate:{slug}] desired_count={count}")


def _fargate_wake(slug):
    """Wake the service with a single atomic update_service call.

    Scales desired_count 0→1 and, only when :latest has been pushed to ECR
    since the current deployment was created, folds in forceNewDeployment
    so the tag re-resolves to the new digest. ECS resolves :latest → digest
    at deployment time and caches it; without forceNewDeployment a bare
    scale-up would reuse the stale cached digest.

    Both fields in one API call → one deployment → one task start, on the
    right image. A separate-call sequence produces two deployments and
    overlapping tasks.

    When the image is unchanged, forceNewDeployment is omitted — a Fargate
    task launch already pulls per-task, but skipping it lets ECS short-circuit
    to the cached digest and keeps wake speed predictable.
    """
    name = _fargate_service_name(slug)
    kwargs = {"cluster": ECS_CLUSTER, "service": name, "desiredCount": 1}
    if _image_stale(slug):
        kwargs["forceNewDeployment"] = True
        print(f"[fargate:{slug}] wake: desired=1 + force-new-deployment (:latest updated)")
    else:
        print(f"[fargate:{slug}] wake: desired=1 (image current)")
    ecs.update_service(**kwargs)


def _image_stale(slug):
    """Return True if :latest has been pushed to ECR after the service's
    current deployment was created. On any check failure, default to True
    so the wake still refreshes rather than serving a potentially stale
    image silently."""
    name = _fargate_service_name(slug)
    try:
        svc_resp = ecs.describe_services(cluster=ECS_CLUSTER, services=[name])
        services = [s for s in svc_resp.get("services", []) if s.get("status") != "INACTIVE"]
        if not services:
            return True
        deployments = services[0].get("deployments", [])
        if not deployments:
            return True
        deploy_created = deployments[0].get("createdAt")

        task_def_arn = services[0]["taskDefinition"]
        td_resp = ecs.describe_task_definition(taskDefinition=task_def_arn)
        image_uri = td_resp["taskDefinition"]["containerDefinitions"][0]["image"]
        # e.g. "123456.dkr.ecr.us-east-1.amazonaws.com/clawless-gateway:latest"
        repo_name = image_uri.split("/", 1)[1].split(":")[0]

        ecr_resp = boto3.client("ecr").describe_images(
            repositoryName=repo_name,
            imageIds=[{"imageTag": "latest"}],
        )
        ecr_pushed = ecr_resp["imageDetails"][0]["imagePushedAt"]
        return bool(deploy_created and ecr_pushed > deploy_created)
    except (ClientError, KeyError, IndexError) as e:
        print(f"[fargate:{slug}] image-stale check failed ({e}) — assuming stale")
        return True


# ── SSM agent config ─────────────────────────────────────────────────────────

def _get_agents():
    """Read /clawless/clients hierarchy from SSM, return {agent_path: config} dict.

    SSM structure:
      /clawless/clients/{client_slug}/{agent_slug}          -> {"client_name": "...", "agent_name": "...", ...}
      /clawless/clients/{client_slug}/{agent_slug}/active   -> "true" or "false"
      /clawless/clients/{client_slug}/{agent_slug}/error    -> error message (if any)

    The /active parameter is split out so agents can pause themselves via a
    tightly scoped IAM policy (ssm:PutParameter on their own /active path only).

    Returns a dict keyed by "{client_slug}/{agent_slug}" with active and _error merged in.
    """
    paginator = ssm.get_paginator("get_parameters_by_path")

    agent_records = {}   # {agent_path: {client_name, agent_name, ...}}
    active_flags = {}    # {agent_path: bool}
    error_flags = {}     # {agent_path: str}

    for page in paginator.paginate(Path="/clawless/clients", Recursive=True, WithDecryption=True):
        for param in page["Parameters"]:
            parts = param["Name"].split("/")
            agent_key = f"{parts[3]}/{parts[4]}"
            # /clawless/clients/{client_slug}/{agent_slug}          -> 5 parts
            # /clawless/clients/{client_slug}/{agent_slug}/active   -> 6 parts
            # /clawless/clients/{client_slug}/{agent_slug}/error    -> 6 parts
            if len(parts) == 5:
                agent_records[agent_key] = json.loads(param["Value"])
            elif len(parts) == 6 and parts[5] == "active":
                active_flags[agent_key] = param["Value"] == "true"
            elif len(parts) == 6 and parts[5] == "error":
                error_flags[agent_key] = param["Value"]

    # Override active from the separate /active parameter, merge _error
    agents = {}
    for agent_path, cfg in agent_records.items():
        agents[agent_path] = {
            **cfg,
            "active": active_flags.get(agent_path, cfg.get("active", True)),
            "_error": error_flags.get(agent_path),
        }

    return agents


# ── Tofu apply ───────────────────────────────────────────────────────────────

def _apply_with_retry(version, agents, apply_slugs, errored_slugs):
    """Run tofu apply with state lock retry (15 attempts, 3s apart)."""
    apply_slugs = apply_slugs - errored_slugs
    if not apply_slugs:
        print("No slugs to apply after filtering errored")
        return

    for attempt in range(1, LOCK_MAX_RETRIES + 2):
        work_dir = tempfile.mkdtemp(dir="/tmp")
        try:
            _apply(work_dir, version, agents, apply_slugs, errored_slugs)
            return  # success
        except StateLockError:
            if attempt > LOCK_MAX_RETRIES:
                print(f"State lock still held after {LOCK_MAX_RETRIES} retries — giving up")
                _send_alert(
                    "Lifecycle apply failed — state lock held",
                    f"Could not acquire tofu state lock after {LOCK_MAX_RETRIES} retries. "
                    f"Slugs: {sorted(apply_slugs)}. Events already consumed — will appear as drift on next run."
                )
                return
            print(f"State lock held — retry {attempt}/{LOCK_MAX_RETRIES} in {LOCK_RETRY_INTERVAL}s")
            time.sleep(LOCK_RETRY_INTERVAL)
        except Exception as e:
            print(f"ERROR: {e}")
            raise
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)


def _apply(work_dir, version, agents, affected_slugs, errored_slugs):
    """Clone repo, init tofu, and run a batched apply for affected agents."""
    # Clone repo at pinned version
    _run(["git", "clone", "--depth=1", "--branch", version, REPO_URL, work_dir])

    tofu_dir = os.path.join(work_dir, "tofu")

    # Reconstruct backend config (not committed to repo)
    with open(os.path.join(tofu_dir, "backend.hcl"), "w") as f:
        f.write(f'bucket = "{STATE_BUCKET}"\n')
        f.write(f'region = "{REGION}"\n')

    # Download tfvars from S3 (uploaded by bootstrap.sh)
    s3.download_file(
        STATE_BUCKET,
        "config/terraform.tfvars",
        os.path.join(tofu_dir, "terraform.tfvars"),
    )

    # Copy read-only plugin cache to writable /tmp so tofu can write lock files
    plugin_dir = "/tmp/tofu-plugin-cache"
    if not os.path.exists(plugin_dir):
        shutil.copytree(PLUGIN_CACHE_DIR, plugin_dir)

    env = {k: v for k, v in os.environ.items() if k != "TF_PLUGIN_CACHE_DIR"}

    _run(
        ["tofu", "init", f"-plugin-dir={plugin_dir}", "-backend-config=backend.hcl", "-input=false"],
        cwd=tofu_dir,
        env=env,
    )

    # Detect removed agents (present in state but absent from SSM).
    state_result = _run(["tofu", "state", "list"], cwd=tofu_dir, env=env)
    state_slugs = _parse_state_slugs(state_result.stdout)
    ssm_slugs = set(agents.keys())
    removed_slugs = state_slugs - ssm_slugs

    if removed_slugs:
        print(f"Detected removed agents: {sorted(removed_slugs)}")
        for slug in sorted(removed_slugs):
            _archive_agent_prefix(slug)

    # Determine which slugs to apply
    all_slugs = ssm_slugs | removed_slugs
    if affected_slugs is not None:
        # Only apply slugs that are actually known (in SSM or state)
        apply_slugs = affected_slugs & all_slugs
    else:
        # Manual invocation — apply all
        apply_slugs = all_slugs

    # Exclude errored slugs
    apply_slugs -= errored_slugs

    if not apply_slugs:
        print("No slugs to apply")
        return

    print(f"Applying {len(apply_slugs)} agent(s): {sorted(apply_slugs)}")

    # Batched apply with multiple -target flags
    target_args = [f'-target=module.client["{slug}"]' for slug in sorted(apply_slugs)]
    try:
        _run(
            ["tofu", "apply", "-auto-approve", "-input=false"] + target_args,
            cwd=tofu_dir,
            env=env,
        )
    except subprocess.CalledProcessError as e:
        _handle_apply_failure(e, apply_slugs, tofu_dir, env)


def _handle_apply_failure(error, apply_slugs, tofu_dir, env):
    """Handle a failed batched tofu apply.

    State lock contention raises StateLockError for the retry loop.
    Real failures: parse slug, taint, write /error, alert, retry remaining.
    """
    stderr = error.stderr or ""
    stdout = error.stdout or ""
    output = stderr + stdout

    # State lock contention — let the retry loop handle it
    if "Error acquiring the state lock" in output:
        raise StateLockError("tofu state lock held by another invocation")

    # Parse failed slugs from tofu error output
    # Tofu errors reference resources like: module.client["test/tess"].aws_ecs_service...
    # Only match slugs on error lines, not warnings
    failed_slugs = set()
    for line in output.splitlines():
        if line.strip().lstrip("│").strip().startswith("Error:"):
            for match in re.finditer(r'module\.client\["([^"]+)"\]', line):
                slug = match.group(1)
                if slug in apply_slugs:
                    failed_slugs.add(slug)

    if not failed_slugs:
        # Couldn't identify specific slugs — treat as systemic
        _send_alert(
            "Lifecycle apply failed — systemic error, could not identify failed slug(s)",
            f"Output:\n{output[-2000:]}"
        )
        raise error

    if len(failed_slugs) > 1:
        # Multiple slugs failed — likely systemic, don't retry
        _send_alert(
            f"Lifecycle apply failed — {len(failed_slugs)} slugs failed, not retrying",
            f"Failed slugs: {sorted(failed_slugs)}\n\nOutput:\n{output[-2000:]}"
        )
        for slug in failed_slugs:
            _mark_error(slug, f"Mass failure: {len(failed_slugs)} slugs failed simultaneously")
            _taint_slug(slug, tofu_dir, env)
        raise error

    # Single slug failed — taint, mark error, exclude, and retry the rest
    failed_slug = failed_slugs.pop()
    print(f"Single slug failed: {failed_slug}")

    _taint_slug(failed_slug, tofu_dir, env)
    _mark_error(failed_slug, output[-1000:])
    _send_alert(
        f"Lifecycle apply failed for {failed_slug} — error state written to SSM",
        f"Slug {failed_slug} failed and has been tainted. Clear /clawless/clients/{failed_slug}/error after investigating.\n\nOutput:\n{output[-1500:]}"
    )

    # Retry remaining slugs
    remaining = apply_slugs - {failed_slug}
    if remaining:
        print(f"Retrying {len(remaining)} remaining slug(s): {sorted(remaining)}")
        target_args = [f'-target=module.client["{slug}"]' for slug in sorted(remaining)]
        try:
            _run(
                ["tofu", "apply", "-auto-approve", "-input=false"] + target_args,
                cwd=tofu_dir,
                env=env,
            )
        except subprocess.CalledProcessError as retry_error:
            # Retry also failed — alert but don't recurse
            _send_alert(
                "Lifecycle apply retry also failed — operator intervention required",
                f"Original failure: {failed_slug}\nRetry failed for: {sorted(remaining)}\n\nOutput:\n{(retry_error.stderr or '')[-1500:]}"
            )
            raise retry_error


def _taint_slug(slug, tofu_dir, env):
    """Taint resources for a failed slug so the next apply recreates them cleanly."""
    try:
        state_result = _run(["tofu", "state", "list"], cwd=tofu_dir, env=env)
        resources = [
            line.strip() for line in state_result.stdout.splitlines()
            if line.strip().startswith(f'module.client["{slug}"]')
        ]
        for resource in resources:
            try:
                _run(["tofu", "taint", resource], cwd=tofu_dir, env=env)
                print(f"Tainted: {resource}")
            except subprocess.CalledProcessError:
                print(f"WARNING: failed to taint {resource}")
    except subprocess.CalledProcessError:
        print(f"WARNING: failed to list state for tainting {slug}")


def _mark_error(slug, error_message):
    """Write an /error parameter to SSM for the given slug."""
    param_name = f"/clawless/clients/{slug}/error"
    # Truncate to fit SSM parameter value limit (4096 bytes)
    value = _strip_ansi(error_message)[:4000] if error_message else "Unknown error"
    try:
        ssm.put_parameter(
            Name=param_name,
            Value=value,
            Type="String",
            Overwrite=True,
        )
        print(f"Wrote error state to {param_name}")
    except ClientError as e:
        print(f"WARNING: failed to write error state to {param_name}: {e}")


def _strip_ansi(text):
    """Remove ANSI escape sequences (color codes, cursor moves, etc.)."""
    return re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text)


def _send_alert(subject, message):
    """Publish an alert to the SNS topic."""
    if not SNS_TOPIC_ARN:
        print(f"ALERT (no SNS topic configured): {subject}")
        return
    try:
        sns.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject=f"[clawless] {subject}"[:100],
            Message=_strip_ansi(message)[:10000],
        )
        print(f"Alert sent: {subject}")
    except ClientError as e:
        print(f"WARNING: failed to send alert: {e}")


# ── Helper functions ─────────────────────────────────────────────────────────

def _archive_agent_prefix(slug):
    """Archive a removed agent's workspace in-place in the shared backup bucket.

    Copies agents/{slug}/* → removed/{slug}/{date}/* inside BACKUP_BUCKET, then
    deletes the original prefix so tofu destroy doesn't trip over it. Idempotent:
    if the source prefix is empty we skip silently.
    """
    src_prefix = f"agents/{slug}/"
    dst_prefix = f"removed/{slug}/{datetime.date.today().isoformat()}/"
    print(f"[remove:{slug}] archiving {src_prefix} → {dst_prefix} in {BACKUP_BUCKET}")

    paginator = s3.get_paginator("list_objects_v2")
    count = 0
    for page in paginator.paginate(Bucket=BACKUP_BUCKET, Prefix=src_prefix):
        for obj in page.get("Contents", []):
            rel = obj["Key"][len(src_prefix):]
            s3.copy_object(
                CopySource={"Bucket": BACKUP_BUCKET, "Key": obj["Key"]},
                Bucket=BACKUP_BUCKET,
                Key=dst_prefix + rel,
            )
            s3.delete_object(Bucket=BACKUP_BUCKET, Key=obj["Key"])
            count += 1
    print(f"[remove:{slug}] archived {count} object(s)")


def _parse_state_slugs(output):
    """Extract agent slugs from `tofu state list` output."""
    slugs = set()
    for line in output.splitlines():
        if line.startswith('module.client["'):
            slug = line.split('"')[1]
            slugs.add(slug)
    return slugs


def _run(cmd, **kwargs):
    print(f"+ {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, flush=True)
    result.check_returncode()
    return result
