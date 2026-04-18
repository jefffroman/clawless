"""Build a SOCI v2 indexed image and promote :latest.

Invoked synchronously by scripts/build-gateway-image.sh after pushing a
candidate tag. Pulls the candidate OCI image into /tmp via crane, runs
`soci convert --standalone` to produce a NEW image containing the SOCI
index (OCI Image Index format, artifactType=application/vnd.amazon.soci.
index.v2+json), pushes it to ECR under :latest, then deletes the
pre-SOCI candidate tag so its digest is orphaned and reaped by the
lifecycle policy.

SOCI v2 replaces the v1 "separate index artifact referencing image
digest" model with "indexed image IS a new image with a new digest".
Fargate's SOCI integration ignores v1 entirely (EOL 2026-02-09), so
this Lambda is what actually lights up lazy-loading.

Event payload:
    repository      — ECR repo name (e.g. "clawless-gateway")
    digest          — pre-SOCI image manifest digest (for logging)
    candidate_tag   — tag to pull from (deleted after promotion)
    promote_tag     — tag to apply to converted image (default "latest")
    region          — AWS region

Returns:
    {"status": "ok", "source_digest", "converted_digest", "promoted_tag"}
Failure: raises; Lambda returns FunctionError to caller, :latest untouched.
"""

import base64
import json
import logging
import os
import shutil
import subprocess

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

WORK_ROOT    = "/tmp/soci-work"
DOCKER_CONFIG_DIR = "/tmp/docker"  # crane reads $DOCKER_CONFIG/config.json


def run(cmd, env=None):
    log.info("exec: %s", " ".join(cmd))
    merged_env = {**os.environ, **(env or {})}
    result = subprocess.run(cmd, capture_output=True, text=True, env=merged_env)
    if result.returncode != 0:
        log.error("exit=%s\nstdout:\n%s\nstderr:\n%s",
                  result.returncode, result.stdout, result.stderr)
        raise RuntimeError(f"{cmd[0]} failed (exit {result.returncode})")
    return result


def write_docker_config(registry, region):
    """Write ~/.docker/config.json-style auth for crane pull/push.

    crane reads $DOCKER_CONFIG/config.json when resolving registry creds.
    We point at /tmp/docker so it's writable under Lambda's RO rootfs."""
    ecr = boto3.client("ecr", region_name=region)
    token_b64 = ecr.get_authorization_token()["authorizationData"][0]["authorizationToken"]
    os.makedirs(DOCKER_CONFIG_DIR, exist_ok=True)
    with open(f"{DOCKER_CONFIG_DIR}/config.json", "w") as f:
        json.dump({"auths": {registry: {"auth": token_b64}}}, f)


def lambda_handler(event, context):
    log.info("event: %s", json.dumps(event))
    repo          = event["repository"]
    source_digest = event["digest"]
    candidate_tag = event["candidate_tag"]
    promote_tag   = event.get("promote_tag", "latest")
    region        = event["region"]

    account_id = boto3.client("sts").get_caller_identity()["Account"]
    registry   = f"{account_id}.dkr.ecr.{region}.amazonaws.com"
    src_ref    = f"{registry}/{repo}:{candidate_tag}"
    dst_ref    = f"{registry}/{repo}:{promote_tag}"

    write_docker_config(registry, region)
    crane_env = {"DOCKER_CONFIG": DOCKER_CONFIG_DIR}

    # Fresh work dir per invocation — warm Lambdas may retain /tmp state
    # and soci convert refuses to write into an existing destination.
    if os.path.exists(WORK_ROOT):
        shutil.rmtree(WORK_ROOT)
    os.makedirs(WORK_ROOT)
    src_layout = f"{WORK_ROOT}/src"
    dst_layout = f"{WORK_ROOT}/dst"

    log.info("pulling %s -> %s", src_ref, src_layout)
    run([
        "crane", "pull",
        "--format", "oci",
        "--platform", "linux/arm64",
        src_ref, src_layout,
    ], env=crane_env)

    log.info("converting to SOCI v2: %s -> %s", src_layout, dst_layout)
    run([
        "soci", "convert",
        "--standalone",
        "--format", "oci-dir",
        src_layout, dst_layout,
    ])

    log.info("pushing %s -> %s", dst_layout, dst_ref)
    run([
        "crane", "push",
        dst_layout, dst_ref,
    ], env=crane_env)

    # Resolve the converted image's digest from the tag we just pushed.
    ecr = boto3.client("ecr", region_name=region)
    converted_digest = ecr.describe_images(
        repositoryName=repo,
        imageIds=[{"imageTag": promote_tag}],
    )["imageDetails"][0]["imageDigest"]
    log.info("promoted :%s -> %s (SOCI v2, was %s)",
             promote_tag, converted_digest, source_digest)

    # Delete the candidate tag. The pre-SOCI digest is now orphaned and
    # will be reaped by the lifecycle policy (keep-last-3). Not fatal if
    # this fails — the tag is harmless and cleanup is eventually-consistent.
    try:
        ecr.batch_delete_image(
            repositoryName=repo,
            imageIds=[{"imageTag": candidate_tag}],
        )
    except Exception as e:
        log.warning("failed to delete candidate tag %s: %s", candidate_tag, e)

    return {
        "status":           "ok",
        "source_digest":    source_digest,
        "converted_digest": converted_digest,
        "promoted_tag":     promote_tag,
    }
