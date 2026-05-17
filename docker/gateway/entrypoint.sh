#!/usr/bin/env bash
# clawless-gateway entrypoint — boot sequence + SIGTERM handling.
#
# Workspace persistence is a SINGLE versioned S3 object per agent:
#   s3://${BACKUP_BUCKET}/agents/${AGENT_SLUG}/workspace.tar.zst
# (tar + zstd-1). History is S3 object versions of that one key — there are
# no dated keys and no per-file fan-out. This keeps the key set bounded so
# the bucket lifecycle can actually expire old state.
#
# Boot order:
#   1. restore: materialise ${WORKSPACE_DIR} from S3, trying in order:
#        (a) the current workspace.tar.zst object,
#        (b) its immediately-prior S3 version (corrupt-archive fallback),
#        (c) the first-boot seed prefix agents/${AGENT_SLUG}/workspace/
#            (the persona scaffold written by seed.tf; only present until
#            the first snapshot is taken),
#        (d) nothing usable anywhere -> hard error (exit 1).
#      Extraction is staged then swapped so a crash mid-restore can never
#      leave a half-populated workspace that later gets snapshotted.
#   2. exec the python gateway as the clawless user. The gateway owns:
#        - chromadb warmup + initial reindex
#        - eager idle-recap of stale sessions
#        - wake_messages DDB drain (claim-deliver-delete)
#        - telegram long-polling
#        - SIGTERM-driven webhook handover
#
# Shutdown order (on SIGTERM from ECS stop_task / sleep tool):
#   1. forward SIGTERM to the python gateway. It stops polling, installs the
#      wake_listener webhook so messages-during-sync route to the Lambda,
#      and exits cleanly.
#   2. snapshot: tar+zstd ${WORKSPACE_DIR} into one temp file and PUT it as a
#      new version of the single key (one s3 cp, retried once). Atomic: the
#      object lands whole or not at all. Failure is non-fatal so sleep never
#      hangs — the prior S3 version remains and is logged for alerting.
#
# Required env vars (provided by the ECS task definition):
#   AGENT_SLUG, BACKUP_BUCKET, AWS_DEFAULT_REGION
# Optional:
#   WORKSPACE_DIR (default /home/clawless), CLAWLESS_VERBOSE

set -euo pipefail

: "${WORKSPACE_DIR:=/home/clawless}"
: "${BACKUP_BUCKET:?BACKUP_BUCKET is required}"
: "${AGENT_SLUG:?AGENT_SLUG is required}"

# Single versioned workspace object.
OBJ_KEY="agents/${AGENT_SLUG}/workspace.tar.zst"
OBJ_URI="s3://${BACKUP_BUCKET}/${OBJ_KEY}"

# First-boot seed prefix: the same bucket, the per-file prefix where
# tofu/modules/client/seed.tf writes the persona scaffold on agent creation.
# Read once, only on the very first boot before any snapshot exists.
SEED_URI="s3://${BACKUP_BUCKET}/agents/${AGENT_SLUG}/workspace/"

# tar-native excludes (paths are relative to WORKSPACE_DIR, archived as ./…).
# Excluding the directory prunes the whole subtree, equivalent to the old
# `.cache/*` sync exclude for capture purposes; the gateway recreates these.
TAR_EXCLUDES=( --exclude='./.cache' --exclude='./.aws' )
# aws-s3-sync-form of the same excludes, used only by the seed fallback.
SEED_SYNC_EXCLUDES=( --exclude '.cache/*' --exclude '.aws/*' )

log() {
  printf '%s [entrypoint] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
}

# ── snapshot (was sync_up): tar+zstd WORKSPACE_DIR -> single versioned key ──
snapshot() {
  log "snapshotting workspace -> ${OBJ_URI}"
  if [ ! -d "$WORKSPACE_DIR" ]; then
    log "snapshot skipped: ${WORKSPACE_DIR} missing (non-fatal)"
    return 0
  fi
  local tmp_tar rc_tar rc_zstd
  tmp_tar="$(mktemp /tmp/clawless-snap.XXXXXX)" || {
    log "snapshot failed: mktemp (non-fatal)"; return 0; }

  # pipefail is on; suspend errexit so we can inspect PIPESTATUS instead of
  # aborting. The python gateway has already exited (waited for, below), so
  # nothing writes the workspace concurrently — tar rc 1 (warnings) is benign.
  set +e
  tar -C "$WORKSPACE_DIR" "${TAR_EXCLUDES[@]}" \
      --warning=no-file-changed -cf - . \
    | zstd -q -1 -T0 -f -o "$tmp_tar"
  rc_tar=${PIPESTATUS[0]}; rc_zstd=${PIPESTATUS[1]}
  set -e

  if { [ "$rc_tar" -ne 0 ] && [ "$rc_tar" -ne 1 ]; } || [ "$rc_zstd" -ne 0 ]; then
    log "snapshot failed: tar=${rc_tar} zstd=${rc_zstd} (non-fatal)"
    rm -f "$tmp_tar"; return 0
  fi

  if aws s3 cp "$tmp_tar" "$OBJ_URI" --no-progress >/dev/null 2>&1; then
    rm -f "$tmp_tar"; log "snapshot complete"; return 0
  fi
  log "snapshot upload failed; retrying once"
  if aws s3 cp "$tmp_tar" "$OBJ_URI" --no-progress >/dev/null 2>&1; then
    rm -f "$tmp_tar"; log "snapshot complete (after retry)"; return 0
  fi
  log "snapshot upload failed after retry (non-fatal)"
  rm -f "$tmp_tar"
  return 0
}

# Extract a verified archive atomically into WORKSPACE_DIR. $1 = archive path.
# Stages into a sibling temp dir (same filesystem -> mv is a rename), and only
# touches WORKSPACE_DIR after a fully successful extract.
_extract_into_workspace() {
  local src="$1" stage
  stage="$(mktemp -d "${WORKSPACE_DIR%/}.stage.XXXXXX")" || return 1
  if ! zstd -dc -q "$src" 2>/dev/null | tar -C "$stage" -xf - --no-same-owner; then
    rm -rf "$stage"; return 1
  fi
  # Destructive swap, only now that the staging tree is complete. Clear the
  # children of WORKSPACE_DIR (keep the directory inode = $HOME stable), then
  # move the staged children in. The single mv pass is the only non-atomic
  # window; it is safe because the same S3 version is untouched and
  # re-restorable next boot, and a half-state is never snapshotted (snapshot
  # only runs at clean shutdown after a successful boot).
  find "$WORKSPACE_DIR" -mindepth 1 -maxdepth 1 -exec rm -rf {} + 2>/dev/null || true
  shopt -s dotglob nullglob
  local staged=( "$stage"/* )
  if [ ${#staged[@]} -gt 0 ]; then
    mv "${staged[@]}" "$WORKSPACE_DIR"/
  fi
  shopt -u dotglob nullglob
  rm -rf "$stage"
  chown -R clawless:clawless "$WORKSPACE_DIR"
  return 0
}

# Download + integrity-check + extract one archive version. $1 = VersionId,
# empty for the current version. 0 = restored, nonzero = unusable.
_try_archive_version() {
  local version_id="$1" tmp_tar
  tmp_tar="$(mktemp /tmp/clawless-restore.XXXXXX)" || return 1
  if [ -z "$version_id" ]; then
    aws s3api get-object --bucket "$BACKUP_BUCKET" --key "$OBJ_KEY" \
      "$tmp_tar" >/dev/null 2>&1 || { rm -f "$tmp_tar"; return 1; }
  else
    aws s3api get-object --bucket "$BACKUP_BUCKET" --key "$OBJ_KEY" \
      --version-id "$version_id" "$tmp_tar" >/dev/null 2>&1 \
      || { rm -f "$tmp_tar"; return 1; }
  fi
  if ! zstd -t -q "$tmp_tar" 2>/dev/null; then
    log "archive (version=${version_id:-current}) failed zstd integrity check"
    rm -f "$tmp_tar"; return 1
  fi
  if ! zstd -dc -q "$tmp_tar" 2>/dev/null | tar -tf - >/dev/null 2>&1; then
    log "archive (version=${version_id:-current}) failed tar listing check"
    rm -f "$tmp_tar"; return 1
  fi
  if ! _extract_into_workspace "$tmp_tar"; then
    log "archive (version=${version_id:-current}) failed to extract"
    rm -f "$tmp_tar"; return 1
  fi
  rm -f "$tmp_tar"
  return 0
}

# True if the single key currently resolves to a real object (head-object
# returns 404 for both a missing key and a delete-marker-as-current).
_object_exists() {
  aws s3api head-object --bucket "$BACKUP_BUCKET" --key "$OBJ_KEY" \
    >/dev/null 2>&1
}

# Newest recoverable noncurrent VersionId for OBJ_KEY, or empty.
#   $1 = 1  current object is present-but-corrupt  -> skip newest (vs[1])
#   $1 = 0  current object is absent/delete-marker  -> newest is good (vs[0])
_prior_version_id() {
  local had_current="$1"
  aws s3api list-object-versions \
    --bucket "$BACKUP_BUCKET" --prefix "$OBJ_KEY" --output json 2>/dev/null \
  | python3 - "$OBJ_KEY" "$had_current" <<'PY' 2>/dev/null || true
import json, sys
key, had = sys.argv[1], sys.argv[2]
data = json.load(sys.stdin)
vs = [v for v in data.get("Versions", []) if v["Key"] == key]
vs.sort(key=lambda v: v["LastModified"], reverse=True)
idx = 1 if had == "1" else 0
print(vs[idx]["VersionId"] if len(vs) > idx else "")
PY
}

# ── restore (was sync_down): (a) current -> (b) prior -> (c) seed -> (d) die ─
restore() {
  log "restoring workspace <- ${OBJ_URI}"
  mkdir -p "$WORKSPACE_DIR"

  # (a) current archive object
  local had_current=0
  if _object_exists; then
    had_current=1
    if _try_archive_version ""; then
      log "restored from current archive"
      return 0
    fi
    log "current archive present but unusable; trying prior version"
  else
    log "no current archive object; trying prior version"
  fi

  # (b) immediately-prior S3 version (corrupt-archive fallback, one step)
  local pv
  pv="$(_prior_version_id "$had_current")"
  if [ -n "$pv" ] && _try_archive_version "$pv"; then
    log "WARNING: restored from PRIOR archive version ${pv}"
    return 0
  fi

  # (c) first boot: no archive yet, pull the seed scaffold per-file
  log "no usable archive; falling back to first-boot seed prefix"
  if aws s3 ls "$SEED_URI" >/dev/null 2>&1; then
    aws s3 sync "$SEED_URI" "$WORKSPACE_DIR/" --no-progress \
      "${SEED_SYNC_EXCLUDES[@]}"
    chown -R clawless:clawless "$WORKSPACE_DIR"
    log "restored from seed prefix (first snapshot writes the archive)"
    return 0
  fi

  # (d) nothing anywhere — fail loudly (matches the old no-prefix guard)
  log "ERROR: no archive, no prior version, no seed prefix at ${SEED_URI}"
  exit 1
}

# Resolve verbose flag from SSM if not explicitly set in env. The lifecycle
# scripts toggle this for live debugging without a redeploy.
load_verbose_flag() {
  if [ -n "${CLAWLESS_VERBOSE:-}" ]; then
    return
  fi
  local val
  val=$(aws ssm get-parameter \
    --name "/clawless/clients/${AGENT_SLUG}/verbose" \
    --query 'Parameter.Value' --output text \
    --region "${AWS_DEFAULT_REGION:-us-east-1}" 2>/dev/null || true)
  case "$val" in
    1|true|yes|on) export CLAWLESS_VERBOSE=1 ;;
  esac
}

gateway_pid=""

shutdown() {
  log "SIGTERM received"
  if [ -n "$gateway_pid" ] && kill -0 "$gateway_pid" 2>/dev/null; then
    kill -TERM "$gateway_pid" 2>/dev/null || true
    wait "$gateway_pid" 2>/dev/null || true
  fi
  snapshot
  log "exiting cleanly"
  exit 0
}
trap shutdown TERM INT

restore
load_verbose_flag

log "starting gateway (verbose=${CLAWLESS_VERBOSE:-0})"
gosu clawless:clawless \
  env HOME="$WORKSPACE_DIR" \
      WORKSPACE_DIR="$WORKSPACE_DIR" \
      MEMORY_DATA_DIR=/var/lib/clawless-memory \
      CLAWLESS_VERBOSE="${CLAWLESS_VERBOSE:-}" \
      /opt/clawless/venv/bin/python -m app.main &
gateway_pid=$!

# Reap if the gateway exits on its own (e.g. fatal config error). We forward
# its exit code to ECS so a crash-loop is visible as failed task health.
wait "$gateway_pid"
rc=$?
log "gateway exited rc=${rc}"
snapshot
exit "$rc"
