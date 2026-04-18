"""Build a SOCI index for a pushed ECR image and promote :latest.

Invoked synchronously by scripts/build-gateway-image.sh after pushing a
candidate tag. Runs a Lambda-local containerd (state under /tmp — rootfs
is read-only except /tmp), fetches image blobs into the content store with
`ctr content fetch` (no snapshotter needed — SOCI reads the raw layers),
runs `soci create` + `soci push` to produce and upload the index as an
OCI artifact referencing the image's manifest digest, then re-tags the
digest as :latest and deletes the candidate tag.

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
SOCI_ROOT         = "/tmp/soci-root"

_containerd_proc = None


def start_containerd():
    """Start containerd as a subprocess using /tmp-based storage.
    Called once per execution environment — reused across warm invocations."""
    global _containerd_proc
    if _containerd_proc and _containerd_proc.poll() is None:
        return

    for d in (CONTAINERD_ROOT, CONTAINERD_STATE, "/tmp/containerd"):
        os.makedirs(d, exist_ok=True)

    # Write a minimal config file (not passable via CLI flags): grpc.uid/gid
    # must match our runtime uid, or containerd's chown-socket step fails
    # with EPERM under Lambda's restricted capability set. ttrpc block sets
    # the same for the shim socket.
    uid, gid = os.getuid(), os.getgid()
    config_path = "/tmp/containerd/config.toml"
    with open(config_path, "w") as f:
        f.write(
            f'version = 2\n'
            f'root  = "{CONTAINERD_ROOT}"\n'
            f'state = "{CONTAINERD_STATE}"\n'
            f'[grpc]\n'
            f'  address = "{CONTAINERD_SOCKET}"\n'
            f'  uid = {uid}\n'
            f'  gid = {gid}\n'
            f'[ttrpc]\n'
            f'  address = "{CONTAINERD_SOCKET}.ttrpc"\n'
            f'  uid = {uid}\n'
            f'  gid = {gid}\n'
        )

    log.info("starting containerd (uid=%d gid=%d)", uid, gid)
    # Capture output so startup failures are diagnosable via CloudWatch.
    containerd_log = open("/tmp/containerd.log", "w")
    _containerd_proc = subprocess.Popen(
        ["containerd", "--config", config_path],
        stdout=containerd_log,
        stderr=subprocess.STDOUT,
    )

    def tail_log():
        try:
            with open("/tmp/containerd.log") as f:
                return f.read()[-4000:]
        except Exception:
            return "(no log captured)"

    for _ in range(60):
        if os.path.exists(CONTAINERD_SOCKET):
            log.info("containerd ready")
            return
        if _containerd_proc.poll() is not None:
            log.error("containerd exited (code=%s) before socket appeared:\n%s",
                      _containerd_proc.returncode, tail_log())
            raise RuntimeError(f"containerd exited code {_containerd_proc.returncode}")
        time.sleep(0.5)

    log.error("containerd timeout (socket never appeared):\n%s", tail_log())
    raise RuntimeError("containerd failed to start within 30s")


def run(cmd, **kw):
    # Redact ECR auth token in logs — it's the last string after --user.
    redacted = [("<redacted>" if i > 0 and cmd[i - 1] == "--user" else c) for i, c in enumerate(cmd)]
    log.info("exec: %s", " ".join(redacted))
    result = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if result.returncode != 0:
        log.error("exit=%s\nstdout:\n%s\nstderr:\n%s",
                  result.returncode, result.stdout, result.stderr)
        raise RuntimeError(f"{redacted[0]} failed (exit {result.returncode})")
    return result


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

    os.makedirs(SOCI_ROOT, exist_ok=True)
    ctr_base  = ["ctr", "--address", CONTAINERD_SOCKET, "--namespace", NAMESPACE]
    soci_base = ["soci", "--address", CONTAINERD_SOCKET, "--namespace", NAMESPACE,
                 "--root", SOCI_ROOT]

    # Fetch blobs only (no unpack). SOCI indexes the content-addressed layers
    # in the store; it doesn't need a snapshot mounted. This sidesteps the
    # snapshotter entirely.
    run(ctr_base + [
        "content", "fetch",
        "--platform", "linux/arm64",
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
