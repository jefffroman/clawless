#!/usr/bin/env bash
# Register a new agent in the /clawless/clients SSM hierarchy.
#
# SSM structure:
#   /clawless/clients/{client_slug}              → {"client_name": "Acme Corp"}
#   /clawless/clients/{client_slug}/{agent_slug} → {agent config}
#
# Client namespace is claimed atomically via --no-overwrite on the client path,
# preventing two different clients from sharing the same slug. If the client
# already exists, the existing client_name is verified to match before proceeding.
#
# Usage: add-agent.sh [--region <region>]
set -euo pipefail

REGION="us-east-1"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --region) REGION="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

hr()  { echo "────────────────────────────────────────────────────────"; }
ask() { # ask <var> <prompt> [default]
  local __var="$1" __prompt="$2" __default="${3:-}" __val=""
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
    echo "Paste the channel_config JSON for this provider (see docs.openclaw.ai/channels)."
    echo "Ensure dmPolicy and allowFrom are set to restrict access to the client."
    read -rp "channel_config JSON: " CHANNEL_CONFIG
    if ! echo "$CHANNEL_CONFIG" | jq . >/dev/null 2>&1; then
      echo "Error: invalid JSON" >&2
      exit 1
    fi
    ;;
esac

# ── Claim client namespace (atomic) ──────────────────────────────────────────
# --no-overwrite on a per-path SSM parameter is atomic: exactly one caller wins
# if two race to register the same client slug simultaneously.
CLIENT_PARAM="/clawless/clients/${CLIENT_SLUG}"
CLIENT_VALUE="$(jq -cn --arg name "$CLIENT_NAME" '{"client_name": $name}')"

if aws ssm put-parameter \
     --name "$CLIENT_PARAM" \
     --type "String" \
     --value "$CLIENT_VALUE" \
     --region "${REGION}" 2>/dev/null; then
  echo "Client namespace '${CLIENT_SLUG}' created."
else
  # Parameter exists — verify it belongs to the same client
  EXISTING_CLIENT=$(aws ssm get-parameter \
    --name "$CLIENT_PARAM" \
    --with-decryption \
    --region "${REGION}" \
    --query 'Parameter.Value' \
    --output text | jq -r '.client_name')
  if [[ "$EXISTING_CLIENT" != "$CLIENT_NAME" ]]; then
    echo "ERROR: Client slug '${CLIENT_SLUG}' is already taken by '${EXISTING_CLIENT}'." >&2
    echo "       Choose a different client name, or use '${EXISTING_CLIENT}' to add an agent to that client." >&2
    exit 1
  fi
  echo "Client namespace '${CLIENT_SLUG}' already exists (${EXISTING_CLIENT}) — adding agent."
fi

# ── Write agent record ────────────────────────────────────────────────────────
AGENT_VALUE="$(jq -cn \
  --arg agent_name     "$AGENT_NAME" \
  --arg agent_channel  "$CHANNEL" \
  --argjson channel_config "$CHANNEL_CONFIG" \
  '{
    agent_name:     $agent_name,
    active:         true,
    agent_channel:  $agent_channel,
    channel_config: $channel_config
  }')"

aws ssm put-parameter \
  --name "/clawless/clients/${CLIENT_SLUG}/${AGENT_SLUG}" \
  --type "SecureString" \
  --value "$AGENT_VALUE" \
  --overwrite \
  --region "${REGION}"

hr
echo "Agent '${AGENT_NAME}' registered at /clawless/clients/${CLIENT_SLUG}/${AGENT_SLUG}."
echo "Resource slug: ${CLIENT_SLUG}-${AGENT_SLUG}"
hr
