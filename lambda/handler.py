"""
Clawless lifecycle Lambda.

Triggered by EventBridge on changes to the /clawless/clients SSM parameter.
Clones the pinned version of the clawless repo and runs `tofu apply` to
reconcile all client infrastructure with the desired state in SSM.

Handles all lifecycle transitions:
  - Add client    (new key in /clawless/clients)
  - Remove client (key deleted from /clawless/clients)
  - Pause client  (active: false)
  - Resume client (active: true)
"""

import json
import os
import shutil
import subprocess
import tempfile

import boto3

ssm = boto3.client("ssm")
s3 = boto3.client("s3")

STATE_BUCKET = os.environ["STATE_BUCKET"]
REPO_URL = os.environ["REPO_URL"]
REGION = os.environ["AWS_DEFAULT_REGION"]
PLUGIN_CACHE_DIR = "/opt/tofu-plugin-cache"


def lambda_handler(event, context):
    print(f"Event: {json.dumps(event)}")

    version = ssm.get_parameter(Name="/clawless/version")["Parameter"]["Value"]
    print(f"Clawless version: {version}")

    work_dir = tempfile.mkdtemp(dir="/tmp")
    try:
        _apply(work_dir, version)
        return {"status": "success"}
    except Exception as e:
        print(f"ERROR: {e}")
        raise
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


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

    env = {**os.environ, "TF_PLUGIN_CACHE_DIR": PLUGIN_CACHE_DIR}

    _run(
        ["tofu", "init", "-backend-config=backend.hcl", "-input=false"],
        cwd=tofu_dir,
        env=env,
    )
    _run(
        ["tofu", "apply", "-auto-approve", "-input=false"],
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
