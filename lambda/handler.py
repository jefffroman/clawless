"""
Clawless lifecycle Lambda.

Triggered by Step Functions on lifecycle changes.  Scripts and the UI invoke SFN
directly; SFN handles the SSM write, writes a pending record to DynamoDB (one per
slug, last-write-wins), and invokes the Lambda only if no other Lambda already owns
that slug.

The Lambda atomically grabs unowned records by setting in_progress=true (conditional
on false).  One Lambda owns each slug until done — no cross-Lambda coordination.

Processing loop prioritises resumes/adds (fast path) over pauses/removals
(slow path).  Pauses and removals are stopped immediately so the agent goes
dark within seconds, but the tofu destroy is batched at the end.

Handles all lifecycle transitions:
  - Add agent    (new path in /clawless/clients/{client}/{agent})
  - Remove agent (path deleted from SSM)
  - Pause agent  (active: false) — stops instance, snapshots, then destroys
  - Resume agent (active: true)  — creates from snapshot, deletes pause snapshot

Failure handling:
  - State lock contention: retry 15 times at 3s intervals (45s max).
  - Single-slug failure: taint partial state, write /error to SSM, exclude slug,
    retry remaining slugs.  SNS alert for every failure.
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
lightsail = boto3.client("lightsail")
ecs = boto3.client("ecs")
sts = boto3.client("sts")

STATE_BUCKET = os.environ["STATE_BUCKET"]
REPO_URL = os.environ["REPO_URL"]
REGION = os.environ["AWS_DEFAULT_REGION"]
LIFECYCLE_TABLE = os.environ.get("LIFECYCLE_TABLE", "")
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")
ECS_CLUSTER = os.environ.get("ECS_CLUSTER", "clawless")
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

    # ── Step 2b: FARGATE DISPATCH — pause/resume via ECS API, no tofu ──
    # Fargate slugs whose ECS service already exists skip the tofu/Lightsail
    # paths: resume is ecs:UpdateService desired=1, pause is desired=0 (the
    # entrypoint's SIGTERM handler runs sync-up and exits cleanly).
    # New fargate slugs (no service yet) still flow through the fast path
    # to let tofu apply create the service; removed fargate slugs still
    # flow through the slow path to let tofu destroy clean up.
    fargate_fast_handled = set()
    fargate_slow_handled = set()

    for slug in list(fast_slugs):
        if _provider(agents, slug) == "fargate" and _fargate_service_exists(slug):
            print(f"[fargate:{slug}] resume via ecs:UpdateService desired=1")
            _fargate_set_desired(slug, 1)
            fargate_fast_handled.add(slug)
    fast_slugs -= fargate_fast_handled

    for slug in list(slow_slugs):
        # Removed agents (slug no longer in SSM) fall through to tofu destroy.
        if slug not in agents:
            continue
        if _provider(agents, slug) == "fargate" and _fargate_service_exists(slug):
            print(f"[fargate:{slug}] pause via ecs:UpdateService desired=0")
            _fargate_set_desired(slug, 0)
            fargate_slow_handled.add(slug)
    slow_slugs -= fargate_slow_handled

    # ── Step 3: FAST PATH — tofu apply for resumes/adds ────────────────
    if fast_slugs:
        print(f"Fast path (resumes/adds): {sorted(fast_slugs)}")
        _apply_with_retry(version, agents, fast_slugs, errored_slugs)

        for slug in fast_slugs:
            if slug in agents and agents[slug].get("active", True):
                if _provider(agents, slug) == "lightsail":
                    _maybe_delete_pause_snapshot(slug)
                    _deregister_managed_instances(slug)

    # ── Step 4: SLOW PATH — stop, snapshot, destroy ────────────────────
    if slow_slugs:
        # Stop all instances immediately (agent goes dark in ~5s).
        # Only Lightsail slugs reach here; Fargate pauses were handled above.
        for slug in sorted(slow_slugs):
            _stop_instance(slug)

        # Start snapshots for pauses (not removals)
        pending_snapshots = {}
        pause_slugs = {s for s in slow_slugs if s in agents}
        for slug in sorted(pause_slugs):
            snap_name = _start_snapshot(slug)
            if snap_name:
                pending_snapshots[snap_name] = slug

        # Wait for snapshots, checking for intent changes during polling
        abandoned = set()
        if pending_snapshots:
            abandoned = _wait_for_snapshots(pending_snapshots, grabbed)

        # Slugs abandoned mid-snapshot (intent changed to resume):
        # instance was stopped but never destroyed (tofu state intact),
        # so just restart it — no tofu apply needed.
        if abandoned:
            print(f"Abandoned pauses (intent changed): {sorted(abandoned)}")
            for slug in abandoned:
                _start_instance(slug)
                slow_slugs.discard(slug)

        # Destroy remaining slow slugs
        destroy_slugs = slow_slugs - abandoned
        if destroy_slugs:
            print(f"Slow path (pauses/removals): {sorted(destroy_slugs)}")
            agents = _get_agents()
            errored_slugs = {slug for slug, cfg in agents.items() if cfg.get("_error")}
            _apply_with_retry(version, agents, destroy_slugs, errored_slugs)

            # Post-destroy: check intent changed during tofu apply
            for slug in list(destroy_slugs):
                if _intent_changed(slug, grabbed[slug]["timestamp"]):
                    print(f"[release:{slug}] intent changed during destroy — re-checking")
                    agents = _get_agents()
                    if slug in agents and agents[slug].get("active", True):
                        print(f"[release:{slug}] now active — recreating via fast path")
                        errored_slugs = {s for s, c in agents.items() if c.get("_error")}
                        _apply_with_retry(version, agents, {slug}, errored_slugs)
                        _maybe_delete_pause_snapshot(slug)
                        _deregister_managed_instances(slug)

            # Wait for destroyed instances to disappear, then clean up MIs
            for slug in destroy_slugs:
                if not _intent_changed(slug, grabbed[slug]["timestamp"]):
                    _wait_for_instance_gone(slug)
                    _deregister_managed_instances(slug, require_online=False)

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


# ── Provider dispatch + Fargate helpers ──────────────────────────────────────

def _provider(agents, slug):
    """Return 'fargate' or 'lightsail' (default) for a given agent slug."""
    cfg = agents.get(slug) or {}
    return cfg.get("provider", "lightsail")


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


# ── Instance stop / start / snapshot ─────────────────────────────────────────

def _stop_instance(agent_path):
    """Stop a Lightsail instance immediately. Makes agent unavailable fast."""
    slug = _resource_slug(agent_path)
    instance_name = f"clawless-{slug}"

    try:
        resp = lightsail.get_instance(instanceName=instance_name)
        state = resp["instance"]["state"]["name"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "NotFoundException":
            print(f"[stop:{slug}] instance not found — nothing to stop")
            return
        raise

    if state == "stopped":
        print(f"[stop:{slug}] already stopped")
        return

    if state != "running":
        print(f"[stop:{slug}] instance state '{state}' — skipping stop")
        return

    print(f"[stop:{slug}] stopping instance...")
    lightsail.stop_instance(instanceName=instance_name)
    print(f"[stop:{slug}] stop requested")


def _start_instance(agent_path):
    """Start a stopped Lightsail instance (used when a resume cancels a pending pause)."""
    slug = _resource_slug(agent_path)
    instance_name = f"clawless-{slug}"

    try:
        resp = lightsail.get_instance(instanceName=instance_name)
        state = resp["instance"]["state"]["name"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "NotFoundException":
            print(f"[start:{slug}] instance not found — nothing to start")
            return
        raise

    if state == "running":
        print(f"[start:{slug}] already running")
        return

    if state not in ("stopped", "stopping"):
        print(f"[start:{slug}] instance state '{state}' — skipping start")
        return

    print(f"[start:{slug}] starting instance...")
    lightsail.start_instance(instanceName=instance_name)
    print(f"[start:{slug}] start requested")


def _start_snapshot(agent_path):
    """Kick off a snapshot for a pause (non-blocking). Returns snapshot name or None."""
    slug = _resource_slug(agent_path)
    instance_name = f"clawless-{slug}"
    snapshot_name = f"clawless-{slug}-snap"

    try:
        lightsail.get_instance(instanceName=instance_name)
    except ClientError as e:
        if e.response["Error"]["Code"] == "NotFoundException":
            print(f"[snapshot:{slug}] instance not found — nothing to snapshot")
            return None
        raise

    # Check if snapshot already exists
    try:
        snap = lightsail.get_instance_snapshot(instanceSnapshotName=snapshot_name)
        snap_state = snap["instanceSnapshot"]["state"]
        if snap_state in ("available", "pending"):
            print(f"[snapshot:{slug}] snapshot already {snap_state}")
            return snapshot_name if snap_state == "pending" else None
    except ClientError as e:
        if e.response["Error"]["Code"] != "NotFoundException":
            raise

    # Wait for instance to be stopped (stop_instance is async)
    for i in range(30):
        inst = lightsail.get_instance(instanceName=instance_name)
        state = inst["instance"]["state"]["name"]
        if state == "stopped":
            break
        if state not in ("stopping", "running"):
            print(f"[snapshot:{slug}] unexpected state '{state}' — skipping snapshot")
            return None
        time.sleep(2)
    else:
        print(f"[snapshot:{slug}] timed out waiting for stopped state — skipping snapshot")
        return None

    print(f"[snapshot:{slug}] creating snapshot {snapshot_name} (non-blocking)...")
    lightsail.create_instance_snapshot(
        instanceName=instance_name,
        instanceSnapshotName=snapshot_name,
    )
    return snapshot_name


def _wait_for_snapshots(snapshot_map, grabbed):
    """Poll until all snapshots are available or their intent changes.

    snapshot_map: {snapshot_name: slug}
    grabbed: {slug: {timestamp, pending}} — used to detect intent changes

    Returns set of slugs whose intent changed (abandoned — should be moved
    to fast path instead of destroyed).
    """
    if not snapshot_map:
        return set()

    print(f"Waiting for {len(snapshot_map)} snapshot(s): {list(snapshot_map.keys())}")
    remaining = dict(snapshot_map)
    abandoned = set()

    while remaining:
        for snap_name, slug in list(remaining.items()):
            # Check if intent changed (new event arrived for this slug)
            if _intent_changed(slug, grabbed[slug]["timestamp"]):
                print(f"[snapshot] {snap_name} — intent changed for {slug}, abandoning")
                remaining.pop(snap_name)
                abandoned.add(slug)
                continue

            try:
                snap = lightsail.get_instance_snapshot(instanceSnapshotName=snap_name)
                state = snap["instanceSnapshot"]["state"]
                if state == "available":
                    print(f"[snapshot] {snap_name} ready")
                    remaining.pop(snap_name)
                elif state == "error":
                    print(f"[snapshot] WARNING: {snap_name} failed")
                    remaining.pop(snap_name)
            except ClientError as e:
                if e.response["Error"]["Code"] == "NotFoundException":
                    print(f"[snapshot] WARNING: {snap_name} not found")
                    remaining.pop(snap_name)
                else:
                    raise
        if remaining:
            time.sleep(10)

    return abandoned


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

    # Download tfvars from S3 (uploaded by bootstrap and bake-snapshot)
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
        account_id = sts.get_caller_identity()["Account"]
        for slug in sorted(removed_slugs):
            _backup_agent_to_shared(slug, account_id)
        _patch_force_destroy(tofu_dir)

    # Agents in SSM but not yet in state are brand new — use golden snapshot.
    new_slugs = ssm_slugs - state_slugs
    new_slugs_var = f"-var=new_agent_slugs={json.dumps(sorted(new_slugs))}"

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
            ["tofu", "apply", "-auto-approve", "-input=false", new_slugs_var] + target_args,
            cwd=tofu_dir,
            env=env,
        )
    except subprocess.CalledProcessError as e:
        _handle_apply_failure(e, apply_slugs, tofu_dir, env, new_slugs_var)


def _handle_apply_failure(error, apply_slugs, tofu_dir, env, new_slugs_var):
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
    # Tofu errors reference resources like: module.client["test/tess"].aws_lightsail_...
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
                ["tofu", "apply", "-auto-approve", "-input=false", new_slugs_var] + target_args,
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

def _wait_for_instance_gone(agent_path, max_wait=300):
    """Poll until a Lightsail instance no longer exists (deleted by tofu)."""
    slug = _resource_slug(agent_path)
    instance_name = f"clawless-{slug}"
    for i in range(max_wait // 5):
        try:
            lightsail.get_instance(instanceName=instance_name)
            time.sleep(5)
        except ClientError as e:
            if e.response["Error"]["Code"] == "NotFoundException":
                print(f"[cleanup:{slug}] instance gone")
                return
            raise
    print(f"WARNING: instance {instance_name} still exists after {max_wait}s")


def _resource_slug(agent_path):
    """Convert 'client/agent' path to 'client-agent' for AWS resource names."""
    return agent_path.replace("/", "-")


def _maybe_delete_pause_snapshot(agent_path):
    """Delete clawless-{slug}-snap after a successful resume (idempotent)."""
    slug = _resource_slug(agent_path)
    snapshot_name = f"clawless-{slug}-snap"

    try:
        lightsail.get_instance_snapshot(instanceSnapshotName=snapshot_name)
    except ClientError as e:
        if e.response["Error"]["Code"] == "NotFoundException":
            return
        raise

    print(f"[resume:{slug}] deleting pause snapshot {snapshot_name}...")
    try:
        lightsail.delete_instance_snapshot(instanceSnapshotName=snapshot_name)
        print(f"[resume:{slug}] snapshot deleted")
    except ClientError as e:
        print(f"[resume:{slug}] WARNING: snapshot delete failed: {e}")


def _deregister_managed_instances(agent_path, require_online=True):
    """Deregister managed instances for this agent's IAM role.

    Each pause/resume cycle creates a new MI ID, leaving the previous one
    orphaned in SSM.

    require_online=True (resume fast path): only deregister offline instances,
      and only if at least one is Online — avoids racing with a freshly booting
      instance that hasn't sent its first ping yet.
    require_online=False (pause/remove slow path): deregister all instances
      unconditionally — the Lightsail instance is destroyed so all MIs will be
      ConnectionLost.
    """
    slug = _resource_slug(agent_path)
    role_name = f"clawless-{slug}-ssm"

    all_instances = []
    paginator = ssm.get_paginator("describe_instance_information")
    for page in paginator.paginate(Filters=[{"Key": "IamRole", "Values": [role_name]}]):
        all_instances.extend(page["InstanceInformationList"])

    if require_online:
        online  = [i for i in all_instances if i["PingStatus"] == "Online"]
        targets = [i for i in all_instances if i["PingStatus"] != "Online"]
        if not online:
            print(f"[cleanup:{slug}] no online instance yet — skipping orphan deregistration")
            return
    else:
        targets = all_instances

    if not targets:
        return

    print(f"[cleanup:{slug}] deregistering {len(targets)} managed instance(s)")
    for instance in targets:
        mi_id = instance["InstanceId"]
        try:
            ssm.deregister_managed_instance(InstanceId=mi_id)
        except ClientError as e:
            print(f"[cleanup:{slug}] WARNING: failed to deregister {mi_id}: {e}")


def _backup_agent_to_shared(agent_path, account_id):
    """Copy all objects from the agent backup prefix into the shared archive bucket."""
    slug = _resource_slug(agent_path)
    src_bucket = f"clawless-{slug}-backup-{account_id}"
    dst_bucket = f"clawless-backups-{account_id}"
    prefix = f"removed/{slug}/{datetime.date.today().isoformat()}/"
    _copy_s3_prefix(src_bucket, dst_bucket, prefix, f"remove:{slug}")


def _copy_s3_prefix(src_bucket, dst_bucket, dst_prefix, label):
    """Copy all objects from src_bucket into dst_bucket under dst_prefix."""
    print(f"[{label}] {src_bucket} -> {dst_bucket}/{dst_prefix}")
    try:
        paginator = s3.get_paginator("list_objects_v2")
        count = 0
        for page in paginator.paginate(Bucket=src_bucket):
            for obj in page.get("Contents", []):
                s3.copy_object(
                    CopySource={"Bucket": src_bucket, "Key": obj["Key"]},
                    Bucket=dst_bucket,
                    Key=dst_prefix + obj["Key"],
                )
                count += 1
        print(f"[{label}] copied {count} objects")
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchBucket":
            print(f"[{label}] WARNING: {src_bucket} not found — skipping")
        else:
            raise


def _patch_force_destroy(tofu_dir):
    """Add force_destroy = true to all aws_s3_bucket resources in the client module."""
    path = os.path.join(tofu_dir, "modules", "client", "main.tf")
    with open(path) as f:
        content = f.read()
    patched = re.sub(
        r'(resource "aws_s3_bucket" "[^"]*" \{)',
        r'\1\n  force_destroy = true',
        content,
    )
    with open(path, "w") as f:
        f.write(patched)
    print(f"[remove] patched force_destroy=true into client module")


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
