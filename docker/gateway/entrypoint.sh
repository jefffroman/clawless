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
#                              (default: openclaw gateway run)
#   OPENCLAW_VERBOSE         — if "1"/"true"/"yes", append --verbose to
#                              OPENCLAW_CMD. Normally sourced from the SSM
#                              parameter /clawless/clients/{slug}/verbose
#                              at boot; a task-def env override wins.
#   WAKE_MESSAGES_TABLE      — DynamoDB table polled at boot for a queued
#                              wake message; unset disables wake-greet
#   MEMORY_REINDEX_INTERVAL  — seconds between reindex checks (default 300)
#   MEMORY_SERVER_URL        — where the context-engine plugin and reindex
#                              loop reach the memory server
#                              (default http://127.0.0.1:3271)
set -euo pipefail

: "${AGENT_SLUG:?AGENT_SLUG is required}"
: "${BACKUP_BUCKET:?BACKUP_BUCKET is required}"
: "${WORKSPACE_DIR:=/home/openclaw}"
: "${OPENCLAW_CMD:=openclaw gateway run}"
: "${OPENCLAW_CONFIG_PATH:=/var/lib/openclaw/openclaw.json}"
: "${OPENCLAW_BASELINE_PATH:=/opt/openclaw/openclaw.baseline.json}"
: "${MEMORY_REINDEX_INTERVAL:=300}"
: "${MEMORY_SERVER_URL:=http://127.0.0.1:3271}"

BACKUP_URI="s3://${BACKUP_BUCKET}/agents/${AGENT_SLUG}/workspace/"

log() { printf '[entrypoint] %s\n' "$*" >&2; }

# Wake-time profiling. Each `phase` call records delta since the previous phase
# and total since entrypoint start. Meant to be ripped out once we've tuned
# boot — grep CloudWatch for `phase=` to collect numbers.
PHASE_T0=$(date +%s.%N)
PHASE_LAST=$PHASE_T0
phase() {
  local now delta total
  now=$(date +%s.%N)
  delta=$(awk -v a="$now" -v b="$PHASE_LAST" 'BEGIN{printf "%.2f", a-b}')
  total=$(awk -v a="$now" -v b="$PHASE_T0" 'BEGIN{printf "%.2f", a-b}')
  log "phase=$1 delta=${delta}s total=${total}s"
  PHASE_LAST=$now
}

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
    --exclude '.openclaw/openclaw.json.bak*' \
    --exclude '.aws/*'
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
    --exclude '.openclaw/agents/*/sessions/*.lock' \
    --exclude '.aws/*' || log "sync-up failed (non-fatal)"
}

load_verbose_flag() {
  # Toggle verbose gateway logging via SSM without a task-def revision.
  # Task role has ssm:GetParameter scoped to /clawless/clients/{slug}/verbose.
  # A task-def env override wins — if OPENCLAW_VERBOSE is already set, skip
  # the SSM read entirely.
  if [ -n "${OPENCLAW_VERBOSE:-}" ]; then
    return 0
  fi
  local val
  val=$(aws ssm get-parameter \
      --name "/clawless/clients/${AGENT_SLUG}/verbose" \
      --query 'Parameter.Value' --output text 2>/dev/null || true)
  case "$val" in
    true|1|yes)
      export OPENCLAW_VERBOSE=1
      log "verbose logging enabled via SSM /clawless/clients/${AGENT_SLUG}/verbose"
      ;;
  esac
}

install_aws_creds() {
  # OpenClaw scrubs AWS_CONTAINER_CREDENTIALS_RELATIVE_URI from tool shells
  # (host-env-security-policy.json: blockedOverrideOnlyKeys), so the agent
  # can't use task-role creds via the ECS metadata hint. Work around it by
  # writing a shared credentials file that uses credential_process to curl
  # the metadata endpoint directly — AWS_CONFIG_FILE/~/.aws/config DO pass
  # through the scrubber. The creds are re-fetched per process invocation,
  # so rotation is automatic. Excluded from S3 sync (per-boot only).
  local uri="${AWS_CONTAINER_CREDENTIALS_RELATIVE_URI:-}"
  if [ -z "$uri" ]; then
    log "install_aws_creds: no relative URI in env — skipping (non-Fargate?)"
    return 0
  fi
  install -d -o openclaw -g openclaw -m 0700 "$WORKSPACE_DIR/.aws"
  cat > "$WORKSPACE_DIR/.aws/ecs-creds.sh" <<EOF
#!/bin/bash
curl -sf "http://169.254.170.2${uri}" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(json.dumps({"Version":1,"AccessKeyId":d["AccessKeyId"],"SecretAccessKey":d["SecretAccessKey"],"SessionToken":d["Token"],"Expiration":d["Expiration"]}))'
EOF
  cat > "$WORKSPACE_DIR/.aws/config" <<EOF
[default]
region = ${AWS_DEFAULT_REGION:-us-east-1}
credential_process = $WORKSPACE_DIR/.aws/ecs-creds.sh
EOF
  chmod 0755 "$WORKSPACE_DIR/.aws/ecs-creds.sh"
  chmod 0644 "$WORKSPACE_DIR/.aws/config"
  chown -R openclaw:openclaw "$WORKSPACE_DIR/.aws"
  log "install_aws_creds: credential_process wired at ~/.aws/config"
}

install_config() {
  # Installed as openclaw:openclaw so the gateway's startup plugin auto-enable
  # write can succeed. lock_config() flips it back to root:root 0444 once the
  # gateway reports healthy. The file lives outside $HOME, so the agent's
  # file tools can't reach it regardless of perms.
  log "installing baseline config → ${OPENCLAW_CONFIG_PATH}"
  install -o openclaw -g openclaw -m 0644 "$OPENCLAW_BASELINE_PATH" "$OPENCLAW_CONFIG_PATH"
}

lock_config() {
  # Wait for the gateway to finish its startup plugin dance, then make the
  # config file immutable for the rest of the session. Runs in the background
  # independently of wake_greet so a skipped greet doesn't block the lock.
  log "lock_config: waiting for gateway health…"
  local elapsed=0
  while ! curl -sf "http://127.0.0.1:18789/health" >/dev/null 2>&1; do
    sleep 3
    elapsed=$((elapsed + 3))
    if [ "$elapsed" -ge 120 ]; then
      log "lock_config: gateway not healthy after 120s — giving up"
      return 0
    fi
  done
  # Small grace period so any post-ready persistence flush completes.
  sleep 2
  # Flip both the file and the containing dir back to root. Without the dir
  # flip openclaw could still atomic-rename-over the locked file because it
  # owns the parent directory.
  chown root:root "$OPENCLAW_CONFIG_PATH" || log "lock_config: chown file failed (non-fatal)"
  chmod 0444 "$OPENCLAW_CONFIG_PATH" || log "lock_config: chmod file failed (non-fatal)"
  chown root:root "$(dirname "$OPENCLAW_CONFIG_PATH")" || log "lock_config: chown dir failed (non-fatal)"
  log "lock_config: config locked"
}

wake_greet() {
  # Post-boot proactive message. If a wake message is queued in DynamoDB,
  # replay it via the openclaw CLI; otherwise send a default "Hello <name>".
  # Sends a greeting or queued message via the openclaw CLI.
  # Runs in the background; all failures are non-fatal.
  if [ -z "${WAKE_MESSAGES_TABLE:-}" ]; then
    log "wake-greet: WAKE_MESSAGES_TABLE unset — skipping"
    return 0
  fi
  if [ -z "${OPENCLAW_CHANNEL:-}" ]; then
    log "wake-greet: OPENCLAW_CHANNEL unset — skipping"
    return 0
  fi

  # Peer id comes from OPENCLAW_CHANNEL_CONFIG.allowFrom[0].
  # No peer → nothing to deliver to.
  local peer_id=""
  if [ -n "${OPENCLAW_CHANNEL_CONFIG:-}" ]; then
    peer_id=$(printf '%s' "$OPENCLAW_CHANNEL_CONFIG" | python3 -c 'import sys,json
try:
    d = json.load(sys.stdin)
    arr = d.get("allowFrom") or []
    print(arr[0] if arr else "")
except Exception:
    print("")' 2>/dev/null || true)
  fi
  if [ -z "$peer_id" ]; then
    log "wake-greet: no peer id in OPENCLAW_CHANNEL_CONFIG.allowFrom — skipping"
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

  # ── Delete Telegram webhook so OpenClaw long-polling takes over ────────
  if [ "$OPENCLAW_CHANNEL" = "telegram" ]; then
    local bot_token=""
    bot_token=$(printf '%s' "$OPENCLAW_CHANNEL_CONFIG" | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("botToken",""))
except: print("")' 2>/dev/null || true)
    if [ -n "$bot_token" ]; then
      log "wake-greet: deleting Telegram webhook…"
      curl -sf -X POST "https://api.telegram.org/bot${bot_token}/deleteWebhook" \
        >/dev/null \
        && log "wake-greet: webhook deleted" \
        || log "wake-greet: WARNING — deleteWebhook failed"
    fi
  fi

  # ── Read and replay queued wake messages from DynamoDB ─────────────────
  local key="{\"slug\": {\"S\": \"${AGENT_SLUG}\"}}"
  local item
  item=$(aws dynamodb get-item \
    --table-name "$WAKE_MESSAGES_TABLE" \
    --key "$key" \
    --output json 2>/dev/null || echo '{}')

  local msg
  # Support list format (messages.L[] from wake listener) and legacy
  # single-message format (message.S from manual DynamoDB writes).
  msg=$(printf '%s' "$item" | python3 -c 'import sys,json
d = json.load(sys.stdin).get("Item") or {}
msgs = d.get("messages",{}).get("L")
if msgs:
    lines = []
    for m in msgs:
        e = m.get("M",{})
        name = e.get("sender_name",{}).get("S","User")
        text = e.get("text",{}).get("S","")
        lines.append(f"{name}: {text}" if text else "")
    print("\n".join(l for l in lines if l))
else:
    print(d.get("message",{}).get("S",""))
' 2>/dev/null || true)

  if [ -n "$msg" ]; then
    log "wake-greet: replaying queued message(s)"
    aws dynamodb delete-item --table-name "$WAKE_MESSAGES_TABLE" --key "$key" >/dev/null 2>&1 || true
  else
    msg="Hello ${AGENT_NAME:-$AGENT_SLUG}"
    log "wake-greet: sending default greeting"
  fi

  gosu openclaw:openclaw openclaw agent \
      --to "$peer_id" \
      --message "$msg" \
      --deliver \
      --channel "$OPENCLAW_CHANNEL" >/dev/null 2>&1 \
    || log "wake-greet: openclaw agent call failed (non-fatal)"
}

memory_server_start() {
  # Launch the long-running aiohttp memory server. Runs as openclaw so its
  # data dir (/var/lib/clawless-memory, chown'd in Dockerfile) is writable.
  # Warms SentenceTransformer + ChromaDB and does an initial reindex before
  # /health returns ok, so by the time the context-engine plugin fires on
  # the first turn the index is ready.
  local server=/opt/clawless/bin/memory_server.py
  if [ ! -r "$server" ]; then
    log "memory-server: not found at ${server} — skipping"
    return 0
  fi
  log "memory-server: starting"
  # Send stdout/stderr to the container's log stream (CloudWatch).
  gosu openclaw:openclaw env \
      AGENT_SLUG="$AGENT_SLUG" \
      WORKSPACE_DIR="$WORKSPACE_DIR" \
      MEMORY_DATA_DIR="/var/lib/clawless-memory" \
      /opt/clawless/memory/venv/bin/python "$server" >&2 &
  memory_server_pid=$!
}

phase_watch_gateway() {
  # Polls gateway /health at 1s cadence and logs when it first responds. Runs
  # in the background so it can't block boot. Paired with phase_watch_memory.
  local t0 now rel total elapsed=0
  t0=$(date +%s.%N)
  while ! curl -sf "http://127.0.0.1:18789/health" >/dev/null 2>&1; do
    sleep 1
    elapsed=$((elapsed + 1))
    if [ "$elapsed" -ge 180 ]; then
      log "phase=gateway_healthy timeout after 180s"
      return 0
    fi
  done
  now=$(date +%s.%N)
  rel=$(awk -v a="$now" -v b="$t0" 'BEGIN{printf "%.2f", a-b}')
  total=$(awk -v a="$now" -v b="$PHASE_T0" 'BEGIN{printf "%.2f", a-b}')
  log "phase=gateway_healthy delta=${rel}s total=${total}s"
}

phase_watch_memory() {
  local t0 now rel total elapsed=0
  t0=$(date +%s.%N)
  while ! curl -sf "${MEMORY_SERVER_URL}/health" >/dev/null 2>&1; do
    sleep 1
    elapsed=$((elapsed + 1))
    if [ "$elapsed" -ge 180 ]; then
      log "phase=memory_healthy timeout after 180s"
      return 0
    fi
  done
  now=$(date +%s.%N)
  rel=$(awk -v a="$now" -v b="$t0" 'BEGIN{printf "%.2f", a-b}')
  total=$(awk -v a="$now" -v b="$PHASE_T0" 'BEGIN{printf "%.2f", a-b}')
  log "phase=memory_healthy delta=${rel}s total=${total}s"
}

memory_reindex_loop() {
  # Background loop: ask the memory server to rebuild its index whenever the
  # workspace source files have changed. The server owns all ChromaDB writes
  # (single writer, no SQLite contention). All failures are non-fatal.
  while true; do
    sleep "$MEMORY_REINDEX_INTERVAL"
    curl -sf -X POST "${MEMORY_SERVER_URL}/reindex" \
        -H 'Content-Type: application/json' \
        -d '{}' >/dev/null 2>&1 \
      || log "reindex: POST /reindex failed (non-fatal)"
  done
}

set_telegram_webhook() {
  # Redirect Telegram messages to the wake listener Lambda so messages
  # sent while the agent is sleeping get queued in DynamoDB.
  # No-op for non-Telegram agents or if WAKE_LISTENER_URL is unset.
  if [ "${OPENCLAW_CHANNEL:-}" != "telegram" ]; then
    return 0
  fi
  if [ -z "${WAKE_LISTENER_URL:-}" ]; then
    log "set_telegram_webhook: WAKE_LISTENER_URL unset — skipping"
    return 0
  fi
  local bot_token=""
  if [ -n "${OPENCLAW_CHANNEL_CONFIG:-}" ]; then
    bot_token=$(printf '%s' "$OPENCLAW_CHANNEL_CONFIG" | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("botToken",""))
except: print("")' 2>/dev/null || true)
  fi
  if [ -z "$bot_token" ]; then
    log "set_telegram_webhook: no botToken in OPENCLAW_CHANNEL_CONFIG — skipping"
    return 0
  fi
  local resource_slug="${AGENT_SLUG//\//-}"
  log "set_telegram_webhook: redirecting to wake listener…"
  curl -sf -X POST "https://api.telegram.org/bot${bot_token}/setWebhook" \
    -d "url=${WAKE_LISTENER_URL}" \
    -d "secret_token=${resource_slug}" \
    -d "allowed_updates=[\"message\"]" \
    >/dev/null \
    && log "set_telegram_webhook: webhook set" \
    || log "set_telegram_webhook: WARNING — setWebhook failed"
}

gateway_pid=""
reindex_pid=""
memory_server_pid=""
shutdown() {
  log "SIGTERM received"
  if [ -n "$reindex_pid" ] && kill -0 "$reindex_pid" 2>/dev/null; then
    kill -TERM "$reindex_pid" 2>/dev/null || true
  fi
  if [ -n "$gateway_pid" ] && kill -0 "$gateway_pid" 2>/dev/null; then
    kill -TERM "$gateway_pid" 2>/dev/null || true
    wait "$gateway_pid" 2>/dev/null || true
  fi
  if [ -n "$memory_server_pid" ] && kill -0 "$memory_server_pid" 2>/dev/null; then
    kill -TERM "$memory_server_pid" 2>/dev/null || true
  fi
  set_telegram_webhook
  sync_up
  log "exiting cleanly"
  exit 0
}
trap shutdown TERM INT

phase "entrypoint_start"
sync_down
phase "sync_down_done"
install_aws_creds
load_verbose_flag
install_config
phase "config_installed"
configure-openclaw
phase "configure_openclaw_done"

case "${OPENCLAW_VERBOSE:-}" in
  1|true|yes) OPENCLAW_CMD="${OPENCLAW_CMD} --verbose" ;;
esac

# Start the memory server first so it has time to warm and index in parallel
# with the gateway's startup; by the time the first turn fires, /retrieve
# is ready. If it never comes up, the plugin falls back to a 2s timeout per
# turn and the agent continues without retrieved context.
memory_server_start
phase_watch_memory &

log "starting openclaw: ${OPENCLAW_CMD}"
gosu openclaw:openclaw $OPENCLAW_CMD &
gateway_pid=$!
phase "openclaw_exec"

phase_watch_gateway &
wake_greet &
lock_config &
memory_reindex_loop &
reindex_pid=$!

wait "$gateway_pid" || true
exit_code=$?
log "openclaw exited with ${exit_code}"
if [ -n "$reindex_pid" ] && kill -0 "$reindex_pid" 2>/dev/null; then
  kill -TERM "$reindex_pid" 2>/dev/null || true
fi
if [ -n "$memory_server_pid" ] && kill -0 "$memory_server_pid" 2>/dev/null; then
  kill -TERM "$memory_server_pid" 2>/dev/null || true
fi
sync_up
exit "$exit_code"
