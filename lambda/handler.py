"""
Clawless lifecycle Lambda.

Triggered by SQS (fed by EventBridge) on changes to the /clawless/clients SSM
hierarchy.  Drains the queue fully, deduplicates affected slugs, reads current
state from SSM, and runs a single batched `tofu apply`.

Handles all lifecycle transitions:
  - Add agent    (new path in /clawless/clients/{client}/{agent})
  - Remove agent (path deleted from SSM)
  - Pause agent  (active: false) — snapshots instance before tofu destroys it
  - Resume agent (active: true)  — deletes pause snapshot after tofu restores it

Failure handling:
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
sqs = boto3.client("sqs")
sns = boto3.client("sns")
lightsail = boto3.client("lightsail")
sts = boto3.client("sts")

STATE_BUCKET = os.environ["STATE_BUCKET"]
REPO_URL = os.environ["REPO_URL"]
REGION = os.environ["AWS_DEFAULT_REGION"]
SQS_QUEUE_URL = os.environ.get("SQS_QUEUE_URL", "")
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")
PLUGIN_CACHE_DIR = "/opt/tofu-plugin-cache"


# ── Entry point ──────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    print(f"Event: {json.dumps(event)}")

    # Determine affected slugs from the event.
    # SQS trigger: drain the queue and extract slugs from all messages.
    # Manual/direct invoke: apply all agents (fallback behavior).
    affected_slugs = _drain_and_extract_slugs(event)

    version = ssm.get_parameter(Name="/clawless/version")["Parameter"]["Value"]
    print(f"Clawless version: {version}")

    agents = _get_agents()

    # Skip agents with an existing /error parameter — operator must clear first.
    errored_slugs = {slug for slug, cfg in agents.items() if cfg.get("_error")}
    if errored_slugs:
        print(f"Skipping agents with /error state: {sorted(errored_slugs)}")

    # Pre-apply: snapshot any instances that are about to be paused.
    for slug, cfg in agents.items():
        if slug in errored_slugs:
            continue
        if not cfg.get("active", True):
            _maybe_snapshot_for_pause(slug)

    work_dir = tempfile.mkdtemp(dir="/tmp")
    try:
        _apply(work_dir, version, agents, affected_slugs, errored_slugs)
    except Exception as e:
        print(f"ERROR: {e}")
        raise
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    # Post-apply: delete pause snapshots and orphaned SSM instances for
    # agents that are active. Both operations are idempotent.
    for slug, cfg in agents.items():
        if slug in errored_slugs:
            continue
        if cfg.get("active", True):
            _maybe_delete_pause_snapshot(slug)
            _deregister_offline_instances(slug)

    return {"status": "success"}


# ── SQS drain & slug extraction ─────────────────────────────────────────────

def _drain_and_extract_slugs(event):
    """Drain all messages from the SQS queue and return the set of affected agent slugs.

    The initial SQS trigger batch is in event["Records"].  We then poll the queue
    until empty to collect any messages that arrived while we were starting up.
    All messages are deleted — they are triggers, not data.

    Returns None if this is a manual/direct invocation (apply all agents).
    """
    records = event.get("Records", [])
    if not records:
        print("No SQS records — manual invocation, will apply all agents")
        return None

    all_bodies = []

    # Process the trigger batch
    for record in records:
        all_bodies.append(record.get("body", "{}"))

    # Drain remaining messages from the queue
    if SQS_QUEUE_URL:
        drained = 0
        receipt_handles = []
        while True:
            resp = sqs.receive_message(
                QueueUrl=SQS_QUEUE_URL,
                MaxNumberOfMessages=10,
                WaitTimeSeconds=0,
            )
            messages = resp.get("Messages", [])
            if not messages:
                break
            for msg in messages:
                all_bodies.append(msg.get("Body", "{}"))
                receipt_handles.append(msg["ReceiptHandle"])
            drained += len(messages)

        # Delete drained messages in batches of 10
        for i in range(0, len(receipt_handles), 10):
            batch = receipt_handles[i:i + 10]
            sqs.delete_message_batch(
                QueueUrl=SQS_QUEUE_URL,
                Entries=[
                    {"Id": str(j), "ReceiptHandle": h}
                    for j, h in enumerate(batch)
                ],
            )

        print(f"Drained {drained} additional messages from queue")

    # Extract slugs from all event bodies
    slugs = set()
    for body_str in all_bodies:
        try:
            body = json.loads(body_str)
            # EventBridge wraps the event; SQS body is the full EventBridge event
            detail = body.get("detail", body)
            param_name = detail.get("name", "")
            parts = param_name.split("/")
            # /clawless/clients/{client}/{agent}[/active|/error] → slug = client/agent
            if len(parts) >= 5:
                slugs.add(f"{parts[3]}/{parts[4]}")
            elif len(parts) == 4:
                # Client-level change — mark for expansion later
                slugs.add(f"__client__{parts[3]}")
        except (json.JSONDecodeError, KeyError, IndexError) as e:
            print(f"WARNING: failed to parse event body: {e}")

    print(f"Affected slugs from {len(all_bodies)} events: {sorted(slugs)}")
    return slugs if slugs else None


# ── SSM agent config ─────────────────────────────────────────────────────────

def _get_agents():
    """Read /clawless/clients hierarchy from SSM, return {agent_path: config} dict.

    SSM structure:
      /clawless/clients/{client_slug}              → {"client_name": "..."}
      /clawless/clients/{client_slug}/{agent_slug} → {"agent_name": "...", "active": true, ...}
      /clawless/clients/{client_slug}/{agent_slug}/error  → error message (if any)

    Returns a dict keyed by "{client_slug}/{agent_slug}" with client_name and _error merged in.
    """
    paginator = ssm.get_paginator("get_parameters_by_path")

    client_records = {}  # {client_slug: {client_name: ...}}
    agent_records = {}   # {agent_path: {agent_name, active, ...}}
    error_flags = {}     # {agent_path: str}

    for page in paginator.paginate(Path="/clawless/clients", Recursive=True, WithDecryption=True):
        for param in page["Parameters"]:
            parts = param["Name"].split("/")
            # /clawless/clients/{client_slug}              → 4 parts
            # /clawless/clients/{client_slug}/{agent_slug} → 5 parts
            # /clawless/clients/{client_slug}/{agent_slug}/error → 6 parts
            if len(parts) == 4:
                client_slug = parts[3]
                client_records[client_slug] = json.loads(param["Value"])
            elif len(parts) == 5:
                client_slug, agent_slug = parts[3], parts[4]
                agent_records[f"{client_slug}/{agent_slug}"] = json.loads(param["Value"])
            elif len(parts) == 6 and parts[5] == "error":
                client_slug, agent_slug = parts[3], parts[4]
                error_flags[f"{client_slug}/{agent_slug}"] = param["Value"]

    # Join client_name and error flag into each agent record
    agents = {}
    for agent_path, cfg in agent_records.items():
        client_slug = agent_path.split("/")[0]
        agents[agent_path] = {
            **cfg,
            "client_name": client_records.get(client_slug, {}).get("client_name", ""),
            "_error": error_flags.get(agent_path),
        }

    return agents


# ── Tofu apply ───────────────────────────────────────────────────────────────

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
        # Expand client-level markers to all agents under that client
        expanded = set()
        for s in affected_slugs:
            if s.startswith("__client__"):
                client = s[len("__client__"):]
                expanded.update(k for k in all_slugs if k.startswith(f"{client}/"))
            else:
                expanded.add(s)
        # Only apply slugs that are actually known (in SSM or state)
        apply_slugs = expanded & all_slugs
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

    Parse the error to identify the failed slug(s).  If exactly one slug failed,
    taint its state, write an /error parameter to SSM, exclude it, and retry the
    remaining slugs.  If multiple slugs failed (systemic issue), alert and stop.
    """
    stderr = error.stderr or ""
    stdout = error.stdout or ""
    output = stderr + stdout

    # Parse failed slugs from tofu error output
    # Tofu errors reference resources like: module.client["test/tess"].aws_lightsail_...
    failed_slugs = set()
    for match in re.finditer(r'module\.client\["([^"]+)"\]', output):
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
    value = error_message[:4000] if error_message else "Unknown error"
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


def _send_alert(subject, message):
    """Publish an alert to the SNS topic."""
    if not SNS_TOPIC_ARN:
        print(f"ALERT (no SNS topic configured): {subject}")
        return
    try:
        sns.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject=f"[clawless] {subject}"[:100],
            Message=message[:10000],
        )
        print(f"Alert sent: {subject}")
    except ClientError as e:
        print(f"WARNING: failed to send alert: {e}")


# ── Helper functions (unchanged) ─────────────────────────────────────────────

def _resource_slug(agent_path):
    """Convert 'client/agent' path to 'client-agent' for AWS resource names."""
    return agent_path.replace("/", "-")


def _maybe_snapshot_for_pause(agent_path):
    """Create clawless-{slug}-snap if the instance is running (pause flow)."""
    slug = _resource_slug(agent_path)
    instance_name = f"clawless-{slug}"
    snapshot_name = f"clawless-{slug}-snap"

    try:
        resp = lightsail.get_instance(instanceName=instance_name)
        state = resp["instance"]["state"]["name"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "NotFoundException":
            print(f"[pause:{slug}] instance not found — nothing to snapshot")
            return
        raise

    if state not in ("running", "stopped"):
        print(f"[pause:{slug}] instance state '{state}' — skipping snapshot")
        return

    try:
        snap = lightsail.get_instance_snapshot(instanceSnapshotName=snapshot_name)
        snap_state = snap["instanceSnapshot"]["state"]
        if snap_state in ("available", "pending"):
            print(f"[pause:{slug}] snapshot already {snap_state} — skipping")
            return
    except ClientError as e:
        if e.response["Error"]["Code"] != "NotFoundException":
            raise

    print(f"[pause:{slug}] creating snapshot {snapshot_name}...")
    lightsail.create_instance_snapshot(
        instanceName=instance_name,
        instanceSnapshotName=snapshot_name,
    )

    print(f"[pause:{slug}] waiting for snapshot to become available...")
    while True:
        snap = lightsail.get_instance_snapshot(instanceSnapshotName=snapshot_name)
        snap_state = snap["instanceSnapshot"]["state"]
        if snap_state == "available":
            break
        if snap_state == "error":
            raise RuntimeError(f"[pause:{slug}] snapshot {snapshot_name} failed")
        time.sleep(10)

    print(f"[pause:{slug}] snapshot ready")


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


def _deregister_offline_instances(agent_path):
    """Deregister offline managed instances for this agent's IAM role (orphan cleanup).

    Each pause/resume cycle creates a new MI ID, leaving the previous one
    orphaned in SSM. This sweeps them after a successful resume.

    Only acts if at least one instance is already Online — avoids racing with
    a freshly booting instance that hasn't sent its first ping yet. Orphans
    that survive this run will be caught on the next lifecycle event.
    """
    slug = _resource_slug(agent_path)
    role_name = f"clawless-{slug}-ssm"

    all_instances = []
    paginator = ssm.get_paginator("describe_instance_information")
    for page in paginator.paginate(Filters=[{"Key": "IamRole", "Values": [role_name]}]):
        all_instances.extend(page["InstanceInformationList"])

    online  = [i for i in all_instances if i["PingStatus"] == "Online"]
    offline = [i for i in all_instances if i["PingStatus"] != "Online"]

    if not online:
        print(f"[cleanup:{slug}] no online instance yet — skipping orphan deregistration")
        return

    for instance in offline:
        mi_id = instance["InstanceId"]
        print(f"[cleanup:{slug}] deregistering orphaned instance {mi_id}")
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
    print(f"[{label}] {src_bucket} → {dst_bucket}/{dst_prefix}")
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
