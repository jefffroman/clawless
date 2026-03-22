"""
Clawless lifecycle Lambda.

Triggered by EventBridge on changes to the /clawless/clients SSM hierarchy.
Clones the pinned version of the clawless repo and runs `tofu apply` to
reconcile all agent infrastructure with the desired state in SSM.

Handles all lifecycle transitions:
  - Add agent    (new path in /clawless/clients/{client}/{agent})
  - Remove agent (path deleted from SSM)
  - Pause agent  (active: false) — snapshots instance before tofu destroys it
  - Resume agent (active: true)  — deletes pause snapshot after tofu restores it
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
lightsail = boto3.client("lightsail")
sts = boto3.client("sts")

STATE_BUCKET = os.environ["STATE_BUCKET"]
REPO_URL = os.environ["REPO_URL"]
REGION = os.environ["AWS_DEFAULT_REGION"]
PLUGIN_CACHE_DIR = "/opt/tofu-plugin-cache"


def lambda_handler(event, context):
    print(f"Event: {json.dumps(event)}")

    version = ssm.get_parameter(Name="/clawless/version")["Parameter"]["Value"]
    print(f"Clawless version: {version}")

    agents = _get_agents()

    # Pre-apply: snapshot any instances that are about to be paused.
    # Idempotent — skips if snapshot already exists or instance is gone.
    for slug, cfg in agents.items():
        if not cfg.get("active", True):
            _maybe_snapshot_for_pause(slug)

    work_dir = tempfile.mkdtemp(dir="/tmp")
    try:
        _apply(work_dir, version, agents)
    except Exception as e:
        print(f"ERROR: {e}")
        raise
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    # Post-apply: delete pause snapshots for agents that were just resumed.
    # Idempotent — skips if no snapshot exists.
    for slug, cfg in agents.items():
        if cfg.get("active", True):
            _maybe_delete_pause_snapshot(slug)

    return {"status": "success"}


def _get_agents():
    """Read /clawless/clients hierarchy from SSM, return {agent_path: config} dict.

    SSM structure:
      /clawless/clients/{client_slug}              → {"client_name": "..."}
      /clawless/clients/{client_slug}/{agent_slug} → {"agent_name": "...", "active": true, ...}

    Returns a dict keyed by "{client_slug}/{agent_slug}" with client_name merged in.
    """
    paginator = ssm.get_paginator("get_parameters_by_path")

    client_records = {}  # {client_slug: {client_name: ...}}
    agent_records = {}   # {agent_path: {agent_name, active, ...}}

    for page in paginator.paginate(Path="/clawless/clients", Recursive=True):
        for param in page["Parameters"]:
            parts = param["Name"].split("/")
            # /clawless/clients/{client_slug}              → 4 parts
            # /clawless/clients/{client_slug}/{agent_slug} → 5 parts
            if len(parts) == 4:
                client_slug = parts[3]
                client_records[client_slug] = json.loads(param["Value"])
            elif len(parts) == 5:
                client_slug, agent_slug = parts[3], parts[4]
                agent_records[f"{client_slug}/{agent_slug}"] = json.loads(param["Value"])

    # Join client_name into each agent record
    agents = {}
    for agent_path, cfg in agent_records.items():
        client_slug = agent_path.split("/")[0]
        agents[agent_path] = {
            **cfg,
            "client_name": client_records.get(client_slug, {}).get("client_name", ""),
        }

    return agents


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


def _apply(work_dir, version, agents):
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

    # Apply each agent individually to reduce error surface
    all_slugs = ssm_slugs | removed_slugs
    for slug in sorted(all_slugs):
        _run(
            ["tofu", "apply", "-auto-approve", "-input=false",
             new_slugs_var,
             f"-target=module.client[\"{slug}\"]"],
            cwd=tofu_dir,
            env=env,
        )


def _run(cmd, **kwargs):
    print(f"+ {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, flush=True)
    result.check_returncode()
    return result
