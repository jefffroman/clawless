"""Build a SOCI index for a pushed ECR image and promote :latest.

Invoked synchronously by scripts/build-gateway-image.sh after pushing a
candidate tag. Pulls the image into a Lambda-local containerd (native
snapshotter, /tmp-backed), runs `soci create` + `soci push` to produce and
upload the index as an OCI artifact referencing the image's manifest digest,
then re-tags the digest as :latest and deletes the candidate tag.

Event payload:
    repository      — ECR repo name (e.g. "clawless-gateway")
    digest          — image manifest digest (sha256:...)
    candidate_tag   — the tag used to push (deleted after promotion)
    promote_tag     — tag to apply post-index (default "latest")
    region          — AWS region

Returns: {"status": "ok", "digest", "promoted_tag"} on success.
Failure: raises; Lambda returns FunctionError to caller, :latest untouched.
"""

import base64
import json
import logging
import os
import subprocess
import time

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

CONTAINERD_SOCKET = "/tmp/containerd/containerd.sock"
CONTAINERD_ROOT   = "/tmp/containerd/root"
CONTAINERD_STATE  = "/tmp/containerd/state"
NAMESPACE         = "soci"

_containerd_proc = None


def start_containerd():
    """Start containerd as a subprocess using /tmp-based storage.
    Called once per execution environment — reused across warm invocations."""
    global _containerd_proc
    if _containerd_proc and _containerd_proc.poll() is None:
        return

    for d in (CONTAINERD_ROOT, CONTAINERD_STATE, "/tmp/containerd"):
        os.makedirs(d, exist_ok=True)

    log.info("starting containerd")
    _containerd_proc = subprocess.Popen(
        ["containerd", "-c", "/etc/containerd/config.toml"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    for _ in range(60):
        if os.path.exists(CONTAINERD_SOCKET):
            log.info("containerd ready")
            return
        time.sleep(0.5)
    raise RuntimeError("containerd failed to start within 30s")


def run(cmd, **kw):
    log.info("exec: %s", " ".join(cmd))
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


def ecr_auth(region):
    """Decoded 'AWS:password' authorization string for ECR (current region)."""
    ecr = boto3.client("ecr", region_name=region)
    token_b64 = ecr.get_authorization_token()["authorizationData"][0]["authorizationToken"]
    return base64.b64decode(token_b64).decode()


def lambda_handler(event, context):
    log.info("event: %s", json.dumps(event))
    repo          = event["repository"]
    digest        = event["digest"]
    candidate_tag = event["candidate_tag"]
    promote_tag   = event.get("promote_tag", "latest")
    region        = event["region"]

    start_containerd()

    account_id = boto3.client("sts").get_caller_identity()["Account"]
    registry   = f"{account_id}.dkr.ecr.{region}.amazonaws.com"
    image_ref  = f"{registry}/{repo}@{digest}"
    auth       = ecr_auth(region)

    ctr_base  = ["ctr", "--address", CONTAINERD_SOCKET, "--namespace", NAMESPACE]
    soci_base = ["soci", "--address", CONTAINERD_SOCKET, "--namespace", NAMESPACE]

    # Pull image via native snapshotter (no overlay/loop needed; regular files).
    run(ctr_base + [
        "images", "pull",
        "--platform", "linux/arm64",
        "--snapshotter", "native",
        "--user", auth,
        image_ref,
    ])

    # Build SOCI index. Defaults: min-layer-size 10MB (skips tiny layers that
    # wouldn't benefit from lazy loading).
    run(soci_base + ["create", image_ref])

    # Push index as OCI artifact referencing the image manifest digest.
    run(soci_base + ["push", "--user", auth, image_ref])

    # Promote: copy the manifest under the new tag. ECR dedupes by digest so
    # this is a pure pointer-write, no blob duplication.
    ecr = boto3.client("ecr", region_name=region)
    manifest = ecr.batch_get_image(
        repositoryName=repo,
        imageIds=[{"imageDigest": digest}],
    )["images"][0]["imageManifest"]
    ecr.put_image(
        repositoryName=repo,
        imageTag=promote_tag,
        imageManifest=manifest,
    )
    log.info("promoted :%s -> %s", promote_tag, digest)

    # Drop the candidate tag so ECR stays tidy. Not fatal if it fails
    # (lifecycle policy will clean up eventually).
    try:
        ecr.batch_delete_image(
            repositoryName=repo,
            imageIds=[{"imageTag": candidate_tag}],
        )
    except Exception as e:
        log.warning("failed to delete candidate tag %s: %s", candidate_tag, e)

    return {
        "status":       "ok",
        "digest":       digest,
        "promoted_tag": promote_tag,
    }
