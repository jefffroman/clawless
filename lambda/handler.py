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

If tofu apply fails for a specific client, that client's SSM entry is updated
with status="error". Error clients are skipped on subsequent runs until an
operator clears the status field and updates SSM to retry.
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


def _is_error(cfg):
    return cfg.get("status") == "error"


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
        if not _is_error(cfg) and not cfg.get("active", True):
            _maybe_snapshot_for_pause(slug)

    work_dir = tempfile.mkdtemp(dir="/tmp")
    failed_slugs = set()
    try:
        failed_slugs = _apply(work_dir, version, clients)
    except Exception as e:
        print(f"ERROR: {e}")
        raise
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    if failed_slugs:
        _mark_clients_error(clients, failed_slugs)

    # Post-apply: delete pause snapshots for clients that were just resumed.
    # Idempotent — skips if no snapshot exists.
    for slug, cfg in clients.items():
        if not _is_error(cfg) and cfg.get("active", True):
            _maybe_delete_pause_snapshot(slug)

    if failed_slugs:
        raise RuntimeError(f"tofu apply failed for: {sorted(failed_slugs)}")

    return {"status": "success"}


def _mark_clients_error(clients, failed_slugs):
    """Write status='error' for failed clients back to SSM.

    This triggers another Lambda run, but error clients are skipped there,
    so the re-run is a no-op for them. The operator must clear status to retry.
    """
    updated = {slug: dict(cfg) for slug, cfg in clients.items()}
    for slug in failed_slugs:
        updated[slug]["status"] = "error"
    ssm.put_parameter(
        Name="/clawless/clients",
        Value=json.dumps(updated),
        Type="String",
        Overwrite=True,
    )
    print(f"Marked as error in SSM: {sorted(failed_slugs)}")


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


def _backup_client_to_shared(slug, account_id):
    """Copy all objects from the client backup bucket into the shared archive bucket."""
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
    """Extract client slugs from `tofu state list` output."""
    slugs = set()
    for line in output.splitlines():
        if line.startswith('module.client["'):
            slug = line.split('"')[1]
            slugs.add(slug)
    return slugs


def _apply(work_dir, version, clients):
    """Run tofu apply for all clients. Returns a set of slugs that failed."""
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

    # Detect removed clients (present in state but absent from SSM).
    # Error clients are kept in ssm_slugs so they are never treated as removed.
    state_result = _run(["tofu", "state", "list"], cwd=tofu_dir, env=env)
    state_slugs = _parse_state_slugs(state_result.stdout)
    ssm_slugs = set(clients.keys())
    removed_slugs = state_slugs - ssm_slugs

    if removed_slugs:
        print(f"Detected removed clients: {sorted(removed_slugs)}")
        account_id = sts.get_caller_identity()["Account"]
        for slug in sorted(removed_slugs):
            _backup_client_to_shared(slug, account_id)
        _patch_force_destroy(tofu_dir)

    # Clients in SSM but not yet in state are brand new — use golden snapshot.
    # Error clients are excluded; they require manual SSM intervention to retry.
    active_ssm_slugs = {s for s, c in clients.items() if not _is_error(c)}
    new_slugs = active_ssm_slugs - state_slugs
    new_slugs_var = f"-var=new_client_slugs={json.dumps(sorted(new_slugs))}"

    # Apply each client individually to limit blast radius.
    # Error clients are excluded; removed clients are included (to be destroyed).
    failed_slugs = set()
    all_slugs = active_ssm_slugs | removed_slugs
    for slug in sorted(all_slugs):
        try:
            _run(
                ["tofu", "apply", "-auto-approve", "-input=false",
                 new_slugs_var,
                 f"-target=module.client[\"{slug}\"]"],
                cwd=tofu_dir,
                env=env,
            )
        except subprocess.CalledProcessError:
            print(f"[{slug}] ERROR: tofu apply failed — will mark as error in SSM")
            failed_slugs.add(slug)

    return failed_slugs


def _run(cmd, **kwargs):
    print(f"+ {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, flush=True)
    result.check_returncode()
    return result
