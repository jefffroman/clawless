#!/usr/bin/env bash
# clawless-gateway entrypoint — boot sequence + SIGTERM handling.
#
# Boot order:
#   1. sync_down: pull workspace from s3://${BACKUP_BUCKET}/agents/${AGENT_SLUG}/workspace/
#      to ${WORKSPACE_DIR}. The agent's MEMORY.md, transcripts, etc. live here.
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
#   2. sync_up: push ${WORKSPACE_DIR} back to S3 with --delete. Volatile
#      paths (chromadb data, locks) are excluded.
#
# Required env vars (provided by the ECS task definition):
#   AGENT_SLUG, BACKUP_BUCKET, AWS_DEFAULT_REGION
# Optional:
#   WORKSPACE_DIR (default /home/clawless), CLAWLESS_VERBOSE

set -euo pipefail

: "${WORKSPACE_DIR:=/home/clawless}"
: "${BACKUP_BUCKET:?BACKUP_BUCKET is required}"
: "${AGENT_SLUG:?AGENT_SLUG is required}"

BACKUP_URI="s3://${BACKUP_BUCKET}/agents/${AGENT_SLUG}/workspace/"

# Volatile excludes: image-baked artifacts that must not overwrite the
# workspace, and per-container state that shouldn't accumulate in S3.
VOLATILE_EXCLUDES=(
  --exclude '.cache/*'
  --exclude '.aws/*'
)

log() {
  printf '%s [entrypoint] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
}

sync_down() {
  log "syncing workspace down from ${BACKUP_URI}"
  if ! aws s3 ls "$BACKUP_URI" >/dev/null 2>&1; then
    log "ERROR: no workspace prefix at ${BACKUP_URI}"
    exit 1
  fi
  aws s3 sync "$BACKUP_URI" "$WORKSPACE_DIR/" --no-progress \
    "${VOLATILE_EXCLUDES[@]}"
  chown -R clawless:clawless "$WORKSPACE_DIR"
}

sync_up() {
  log "syncing workspace up to ${BACKUP_URI}"
  aws s3 sync "$WORKSPACE_DIR/" "$BACKUP_URI" --delete --no-progress \
    "${VOLATILE_EXCLUDES[@]}" \
    || log "sync-up failed (non-fatal)"
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
  sync_up
  log "exiting cleanly"
  exit 0
}
trap shutdown TERM INT

sync_down
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
sync_up
exit "$rc"
