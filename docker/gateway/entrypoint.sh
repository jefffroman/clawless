#!/usr/bin/env bash
# Gateway container entrypoint. Runs as root (PID 1 via tini) so it can write
# a root-owned openclaw.json that the openclaw user can read but not modify.
# Drops to the openclaw user via gosu before starting the gateway.
#
# Flow:
#   1. Sync $WORKSPACE_DIR down from S3 backup bucket, chown to openclaw
#   2. Install fresh baseline → $OPENCLAW_CONFIG_PATH (root:root 0644)
#   3. Patch with per-client env-driven config via configure-openclaw (as root)
#   4. Start openclaw gateway as the openclaw user
#   5. Kick off wake-greet and memory reindex background helpers
#   6. On SIGTERM: stop gateway, sync workspace back up, exit
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
#   WAKE_MESSAGES_TABLE      — DynamoDB table polled at boot for a queued
#                              wake message; unset disables wake-greet
#   MEMORY_REINDEX_INTERVAL  — seconds between indexer runs (default 300)
set -euo pipefail

: "${AGENT_SLUG:?AGENT_SLUG is required}"
: "${BACKUP_BUCKET:?BACKUP_BUCKET is required}"
: "${WORKSPACE_DIR:=/home/openclaw}"
: "${OPENCLAW_CMD:=openclaw gateway}"
: "${OPENCLAW_CONFIG_PATH:=/var/lib/openclaw/openclaw.json}"
: "${OPENCLAW_BASELINE_PATH:=/opt/openclaw/openclaw.baseline.json}"
: "${MEMORY_REINDEX_INTERVAL:=300}"

BACKUP_URI="s3://${BACKUP_BUCKET}/agents/${AGENT_SLUG}/workspace/"

log() { printf '[entrypoint] %s\n' "$*" >&2; }

sync_down() {
  log "syncing workspace down from ${BACKUP_URI}"
  if ! aws s3 ls "$BACKUP_URI" >/dev/null 2>&1; then
    log "ERROR: no workspace found at ${BACKUP_URI}"
    log "provisioning must seed the S3 prefix before the task starts"
    exit 1
  fi
  # openclaw.json used to live under .openclaw/ in the workspace; it is now
  # delivered via the container image. Exclude any stale copy so it can't
  # override the root-owned file we install next.
  aws s3 sync "$BACKUP_URI" "$WORKSPACE_DIR/" --no-progress \
    --exclude '.openclaw/openclaw.json' \
    --exclude '.openclaw/openclaw.json.bak*'
  chown -R openclaw:openclaw "$WORKSPACE_DIR"
}

sync_up() {
  log "syncing workspace up to ${BACKUP_URI}"
  aws s3 sync "$WORKSPACE_DIR/" "$BACKUP_URI" --delete --no-progress \
    --exclude 'vector_memory/venv/*' \
    --exclude 'vector_memory/chroma_db/*' \
    --exclude 'vector_memory/__pycache__/*' \
    --exclude '.openclaw/openclaw.json' \
    --exclude '.openclaw/openclaw.json.bak*' \
    --exclude '.openclaw/agents/*/sessions/*.lock' || log "sync-up failed (non-fatal)"
}

install_config() {
  log "installing baseline config → ${OPENCLAW_CONFIG_PATH}"
  install -o root -g root -m 0644 "$OPENCLAW_BASELINE_PATH" "$OPENCLAW_CONFIG_PATH"
}

wake_greet() {
  # Post-boot proactive message. If a wake message is queued in DynamoDB,
  # replay it via the openclaw CLI; otherwise send a default greeting.
  # Runs in the background; all failures are non-fatal.
  if [ -z "${WAKE_MESSAGES_TABLE:-}" ]; then
    log "wake-greet: WAKE_MESSAGES_TABLE unset — skipping"
    return 0
  fi
  if [ -z "${OPENCLAW_CHANNEL:-}" ]; then
    log "wake-greet: OPENCLAW_CHANNEL unset — skipping"
    return 0
  fi

  log "wake-greet: waiting for gateway health…"
  local elapsed=0
  while ! curl -sf "http://127.0.0.1:18789/health" >/dev/null 2>&1; do
    sleep 3
    elapsed=$((elapsed + 3))
    if [ "$elapsed" -ge 120 ]; then
      log "wake-greet: gateway not healthy after 120s — giving up"
      return 0
    fi
  done
  log "wake-greet: gateway healthy after ${elapsed}s"

  local key="{\"slug\": {\"S\": \"${AGENT_SLUG}\"}}"
  local item
  item=$(aws dynamodb get-item \
    --table-name "$WAKE_MESSAGES_TABLE" \
    --key "$key" \
    --output json 2>/dev/null || echo '{}')

  local msg
  msg=$(printf '%s' "$item" | python3 -c 'import sys,json; d=json.load(sys.stdin); print((d.get("Item") or {}).get("message",{}).get("S",""))' 2>/dev/null || true)

  if [ -n "$msg" ]; then
    log "wake-greet: replaying queued message"
    aws dynamodb delete-item --table-name "$WAKE_MESSAGES_TABLE" --key "$key" >/dev/null 2>&1 || true
  else
    msg="(woken up — no queued message)"
    log "wake-greet: sending default greeting"
  fi

  gosu openclaw:openclaw openclaw agent --message "$msg" >/dev/null 2>&1 \
    || log "wake-greet: openclaw agent call failed (non-fatal)"
}

memory_reindex_loop() {
  # Background loop: rebuild the ChromaDB index from MEMORY.md every
  # MEMORY_REINDEX_INTERVAL seconds. Runs as openclaw so output files are
  # agent-readable. All failures are logged but non-fatal.
  local indexer=/opt/clawless/bin/indexer.py
  if [ ! -x "$indexer" ]; then
    log "reindex: indexer not found at ${indexer} — skipping"
    return 0
  fi
  while true; do
    sleep "$MEMORY_REINDEX_INTERVAL"
    gosu openclaw:openclaw /opt/clawless/memory/venv/bin/python "$indexer" >/dev/null 2>&1 \
      || log "reindex: run failed (non-fatal)"
  done
}

gateway_pid=""
reindex_pid=""
shutdown() {
  log "SIGTERM received"
  if [ -n "$reindex_pid" ] && kill -0 "$reindex_pid" 2>/dev/null; then
    kill -TERM "$reindex_pid" 2>/dev/null || true
  fi
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
install_config
configure-openclaw

log "starting openclaw: ${OPENCLAW_CMD}"
gosu openclaw:openclaw $OPENCLAW_CMD &
gateway_pid=$!

wake_greet &
memory_reindex_loop &
reindex_pid=$!

wait "$gateway_pid" || true
exit_code=$?
log "openclaw exited with ${exit_code}"
if [ -n "$reindex_pid" ] && kill -0 "$reindex_pid" 2>/dev/null; then
  kill -TERM "$reindex_pid" 2>/dev/null || true
fi
sync_up
exit "$exit_code"
