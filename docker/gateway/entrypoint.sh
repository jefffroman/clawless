#!/usr/bin/env bash
# Gateway container entrypoint. Runs as `openclaw` (uid 1000) with HOME=$WORKSPACE_DIR.
#
# The workspace (including ~/.openclaw/openclaw.json) is the source of truth.
# Provisioning (initial creation of a client's workspace) happens outside the
# container — the lifecycle Lambda seeds the S3 prefix before the task ever
# starts. If sync-down finds no config, we exit hard rather than guess.
#
# Flow:
#   1. Sync $WORKSPACE_DIR down from S3 backup bucket
#   2. Assert ~/.openclaw/openclaw.json exists
#   3. Patch it with per-client config via configure-openclaw (env-driven)
#   4. Start openclaw gateway in the background
#   5. On SIGTERM: stop gateway, sync workspace back up, exit
#
# Required env vars (from ECS task definition):
#   AGENT_SLUG               — client identifier used in S3 path
#   BACKUP_BUCKET            — e.g. clawless-backups-${account}
#   OPENCLAW_GATEWAY_TOKEN   — gateway auth token (injected by task def)
#   OPENCLAW_MODEL           — bedrock model string
#   OPENCLAW_CHANNEL         — e.g. "telegram"
#   OPENCLAW_CHANNEL_CONFIG  — JSON blob for channels.{channel}
#   AWS_DEFAULT_REGION       — bedrock region
#
# Optional:
#   OPENCLAW_CMD             — override command to start the gateway
#                              (default: openclaw gateway)
set -euo pipefail

: "${AGENT_SLUG:?AGENT_SLUG is required}"
: "${BACKUP_BUCKET:?BACKUP_BUCKET is required}"
: "${WORKSPACE_DIR:=/home/openclaw}"
: "${OPENCLAW_CMD:=openclaw gateway}"

BACKUP_URI="s3://${BACKUP_BUCKET}/agents/${AGENT_SLUG}/workspace/"

log() { printf '[entrypoint] %s\n' "$*" >&2; }

sync_down() {
  log "syncing workspace down from ${BACKUP_URI}"
  if ! aws s3 ls "$BACKUP_URI" >/dev/null 2>&1; then
    log "ERROR: no workspace found at ${BACKUP_URI}"
    log "provisioning must seed the S3 prefix before the task starts"
    exit 1
  fi
  aws s3 sync "$BACKUP_URI" "$WORKSPACE_DIR/" --no-progress
}

sync_up() {
  log "syncing workspace up to ${BACKUP_URI}"
  aws s3 sync "$WORKSPACE_DIR/" "$BACKUP_URI" --delete --no-progress \
    --exclude 'vector_memory/venv/*' \
    --exclude 'vector_memory/chroma_db/*' \
    --exclude 'vector_memory/__pycache__/*' \
    --exclude '.openclaw/agents/*/sessions/*.lock' || log "sync-up failed (non-fatal)"
}

assert_config() {
  if [ ! -f "$OPENCLAW_CONFIG_PATH" ]; then
    log "ERROR: ${OPENCLAW_CONFIG_PATH} missing after sync-down"
    log "workspace state is incomplete — provisioning did not seed openclaw.json"
    exit 1
  fi
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
assert_config
configure-openclaw

log "starting openclaw: ${OPENCLAW_CMD}"
$OPENCLAW_CMD &
gateway_pid=$!
wait "$gateway_pid" || true
exit_code=$?
log "openclaw exited with ${exit_code}"
sync_up
exit "$exit_code"
