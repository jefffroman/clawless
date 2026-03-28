#!/usr/bin/env bash
# Register a new agent in the /clawless/clients SSM hierarchy.
#
# SSM structure:
#   /clawless/clients/{client_slug}/{agent_slug} → {agent config incl. client_name}
#
# Client slug uniqueness is enforced by the storefront (clawless-platform).
#
# Usage: add-agent.sh [options]
#
# Options:
#   --region <region>          AWS region (default: us-east-1)
#   --client-name <name>       Client name (e.g. "Acme Corp")
#   --agent-name <name>        Agent name (e.g. "Aria")
#   --channel <type>           Channel type: telegram / discord / slack / other
#   --bot-token <token>        Bot token (telegram/discord/slack)
#   --peer-id <id>             Client user ID for the channel
#   --app-token <token>        Slack app token (xapp-...) — slack only
#   --channel-config <json>    Raw channel config JSON — overrides individual fields
#
# Any parameter not supplied on the command line will be prompted interactively.
set -euo pipefail

REGION="us-east-1"
CLIENT_NAME="" AGENT_NAME="" CHANNEL="" BOT_TOKEN="" PEER_ID="" APP_TOKEN=""
CHANNEL_CONFIG_RAW=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region)         REGION="$2";           shift 2 ;;
    --client-name)    CLIENT_NAME="$2";      shift 2 ;;
    --agent-name)     AGENT_NAME="$2";       shift 2 ;;
    --channel)        CHANNEL="$2";          shift 2 ;;
    --bot-token)      BOT_TOKEN="$2";        shift 2 ;;
    --peer-id)        PEER_ID="$2";          shift 2 ;;
    --app-token)      APP_TOKEN="$2";        shift 2 ;;
    --channel-config) CHANNEL_CONFIG_RAW="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

hr()  { echo "────────────────────────────────────────────────────────"; }
ask() { # ask <var> <prompt> [default]
  local __var="$1" __prompt="$2" __default="${3:-}" __val=""
  # Skip prompt if variable already set via CLI
  eval "__val=\${$__var:-}"
  if [[ -n "$__val" ]]; then
    return
  fi
  if [[ -n "$__default" ]]; then
    read -rp "${__prompt} [${__default}]: " __val
    printf -v "$__var" '%s' "${__val:-$__default}"
  else
    while [[ -z "$__val" ]]; do
      read -rp "${__prompt}: " __val
    done
    printf -v "$__var" '%s' "$__val"
  fi
}

slugify() {
  echo "$1" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]/-/g' | sed 's/-\+/-/g' | sed 's/^-\|-$//g'
}

# ── Client identity ───────────────────────────────────────────────────────────
ask CLIENT_NAME "Client name (e.g. Acme Corp)"
CLIENT_SLUG="$(slugify "$CLIENT_NAME")"
echo "  Client slug: ${CLIENT_SLUG}"

# ── Agent identity ────────────────────────────────────────────────────────────
ask AGENT_NAME "Agent name (e.g. Aria)"
AGENT_SLUG="$(slugify "$AGENT_NAME")"
echo "  Agent slug:  ${AGENT_SLUG}"
echo "  SSM path:    /clawless/clients/${CLIENT_SLUG}/${AGENT_SLUG}"

# ── Channel ───────────────────────────────────────────────────────────────────
hr
ask CHANNEL "Channel (telegram / discord / slack / other)" "telegram"
CHANNEL="$(echo "$CHANNEL" | tr '[:upper:]' '[:lower:]')"

if [[ -n "$CHANNEL_CONFIG_RAW" ]]; then
  # Validate raw JSON if provided via CLI
  if ! echo "$CHANNEL_CONFIG_RAW" | jq . >/dev/null 2>&1; then
    echo "Error: --channel-config is not valid JSON" >&2
    exit 1
  fi
  CHANNEL_CONFIG="$CHANNEL_CONFIG_RAW"
else
  case "$CHANNEL" in
    telegram)
      ask BOT_TOKEN "Agent Bot token"
      ask PEER_ID "Client Telegram numeric user ID"
      CHANNEL_CONFIG="$(jq -cn \
        --arg token "$BOT_TOKEN" \
        --arg peer  "$PEER_ID" \
        '{"enabled": true, "botToken": $token, "dmPolicy": "allowlist", "allowFrom": [$peer]}')"
      ;;
    discord)
      ask BOT_TOKEN "Agent Bot token"
      ask PEER_ID "Client Discord numeric user ID"
      CHANNEL_CONFIG="$(jq -cn \
        --arg token "$BOT_TOKEN" \
        --arg peer  "$PEER_ID" \
        '{"enabled": true, "token": $token, "dmPolicy": "allowlist", "allowFrom": [("user:" + $peer)]}')"
      ;;
    slack)
      echo "Create a Slack app with socket mode enabled."
      ask APP_TOKEN "App token (xapp-...)"
      ask BOT_TOKEN "Agent Bot token (xoxb-...)"
      ask PEER_ID "Client Slack member ID (U...)"
      CHANNEL_CONFIG="$(jq -cn \
        --arg app  "$APP_TOKEN" \
        --arg bot  "$BOT_TOKEN" \
        --arg peer "$PEER_ID" \
        '{"enabled": true, "mode": "socket", "appToken": $app, "botToken": $bot, "dmPolicy": "allowlist", "allowFrom": [$peer]}')"
      ;;
    *)
      if [[ -z "$CHANNEL_CONFIG_RAW" ]]; then
        echo "Paste the channel_config JSON for this provider (see docs.openclaw.ai/channels)."
        echo "Ensure dmPolicy and allowFrom are set to restrict access to the client."
        read -rp "channel_config JSON: " CHANNEL_CONFIG
        if ! echo "$CHANNEL_CONFIG" | jq . >/dev/null 2>&1; then
          echo "Error: invalid JSON" >&2
          exit 1
        fi
      fi
      ;;
  esac
fi

# ── Write agent record ────────────────────────────────────────────────────────
AGENT_VALUE="$(jq -cn \
  --arg client_name    "$CLIENT_NAME" \
  --arg agent_name     "$AGENT_NAME" \
  --arg agent_channel  "$CHANNEL" \
  --argjson channel_config "$CHANNEL_CONFIG" \
  '{
    client_name:    $client_name,
    agent_name:     $agent_name,
    active:         true,
    agent_channel:  $agent_channel,
    channel_config: $channel_config
  }')"

AGENT_PARAM="/clawless/clients/${CLIENT_SLUG}/${AGENT_SLUG}"

echo "Invoking Step Functions (SSM write + lifecycle)..."
SFN_ARN=$(aws stepfunctions list-state-machines --region "$REGION" \
  --query 'stateMachines[?name==`clawless-lifecycle`].stateMachineArn | [0]' --output text)
SFN_INPUT=$(jq -cn \
  --arg name "$AGENT_PARAM" \
  --arg time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg val  "$AGENT_VALUE" \
  '{event_id: (now | tostring), time: $time, name: $name, operation: "Create", ssm_value: $val}')
aws stepfunctions start-execution \
  --state-machine-arn "$SFN_ARN" \
  --input "$SFN_INPUT" \
  --region "$REGION" >/dev/null
echo "Step Functions invoked."

hr
echo "Agent '${AGENT_NAME}' registered at ${AGENT_PARAM}."
echo "Resource slug: ${CLIENT_SLUG}-${AGENT_SLUG}"
hr
