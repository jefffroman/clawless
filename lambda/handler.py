"""
Clawless lifecycle Lambda.

Triggered by EventBridge on changes to the /clawless/clients SSM parameter.
Clones the pinned version of the clawless repo and runs `tofu apply` to
reconcile all client infrastructure with the desired state in SSM.

Handles all lifecycle transitions:
  - Add client    (new key in /clawless/clients)
  - Remove client (key deleted from /clawless/clients)
  - Pause client  (active: false) — snapshots instance before tofu destroys it
  - Resume client (active: true)  — deletes pause snapshot after tofu restores it
"""

import json
import os
import shutil
import subprocess
import tempfile
import time

import boto3
from botocore.exceptions import ClientError

ssm = boto3.client("ssm")
s3 = boto3.client("s3")
lightsail = boto3.client("lightsail")

STATE_BUCKET = os.environ["STATE_BUCKET"]
REPO_URL = os.environ["REPO_URL"]
REGION = os.environ["AWS_DEFAULT_REGION"]
PLUGIN_CACHE_DIR = "/opt/tofu-plugin-cache"


def lambda_handler(event, context):
    print(f"Event: {json.dumps(event)}")

    version = ssm.get_parameter(Name="/clawless/version")["Parameter"]["Value"]
    print(f"Clawless version: {version}")

    clients = json.loads(
        ssm.get_parameter(Name="/clawless/clients")["Parameter"]["Value"]
    )

    # Pre-apply: snapshot any instances that are about to be paused.
    # Idempotent — skips if snapshot already exists or instance is gone.
    for slug, cfg in clients.items():
        if not cfg.get("active", True):
            _maybe_snapshot_for_pause(slug)

    work_dir = tempfile.mkdtemp(dir="/tmp")
    try:
        _apply(work_dir, version)
    except Exception as e:
        print(f"ERROR: {e}")
        raise
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    # Post-apply: delete pause snapshots for clients that were just resumed.
    # Idempotent — skips if no snapshot exists.
    for slug, cfg in clients.items():
        if cfg.get("active", True):
            _maybe_delete_pause_snapshot(slug)

    return {"status": "success"}


def _maybe_snapshot_for_pause(slug):
    """Create clawless-{slug}-snap if the instance is running (pause flow)."""
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


def _maybe_delete_pause_snapshot(slug):
    """Delete clawless-{slug}-snap after a successful resume (idempotent)."""
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


def _apply(work_dir, version):
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
    _run(
        ["tofu", "apply", "-auto-approve", "-input=false", "-target=module.client"],
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
