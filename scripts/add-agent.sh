#!/usr/bin/env bash
# Add or update an agent in the /clawless/clients SSM parameter.
# Called by bootstrap.sh for the first agent; run directly to add more.
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
ask DISPLAY_NAME "Client display name (e.g. Acme Corp)"
SLUG="$(slugify "$DISPLAY_NAME")"
echo "  Client slug: ${SLUG}"

# ── Agent identity ────────────────────────────────────────────────────────────
ask AGENT_NAME "Agent name (e.g. Aria, Max)"

# ── Channel ───────────────────────────────────────────────────────────────────
hr
ask CHANNEL "Channel (telegram / discord / slack / other)" "telegram"
CHANNEL="${CHANNEL,,}"  # lowercase

case "$CHANNEL" in
  telegram)
    echo "Create a bot via @BotFather on Telegram to get a token."
    ask BOT_TOKEN "Bot token"
    CHANNEL_CONFIG="$(jq -cn \
      --arg token "$BOT_TOKEN" \
      '{"enabled": true, "botToken": $token, "dmPolicy": "pairing"}')"
    ;;
  discord)
    echo "Create a bot at discord.com/developers and copy the bot token."
    ask BOT_TOKEN "Bot token"
    CHANNEL_CONFIG="$(jq -cn \
      --arg token "$BOT_TOKEN" \
      '{"enabled": true, "token": $token}')"
    ;;
  slack)
    echo "Create a Slack app with socket mode enabled."
    ask APP_TOKEN "App token (xapp-...)"
    ask BOT_TOKEN "Bot token (xoxb-...)"
    CHANNEL_CONFIG="$(jq -cn \
      --arg app "$APP_TOKEN" \
      --arg bot "$BOT_TOKEN" \
      '{"enabled": true, "mode": "socket", "appToken": $app, "botToken": $bot}')"
    ;;
  *)
    echo "Paste the channel_config JSON for this provider (see docs.openclaw.ai/channels):"
    read -rp "channel_config JSON: " CHANNEL_CONFIG
    if ! echo "$CHANNEL_CONFIG" | jq . >/dev/null 2>&1; then
      echo "Error: invalid JSON" >&2
      exit 1
    fi
    ;;
esac

# ── Build client entry ────────────────────────────────────────────────────────
CLIENT_JSON="$(jq -cn \
  --arg display_name   "$DISPLAY_NAME" \
  --arg agent_name     "$AGENT_NAME" \
  --arg agent_channel  "$CHANNEL" \
  --argjson channel_config "$CHANNEL_CONFIG" \
  '{
    display_name:   $display_name,
    active:         true,
    agent_name:     $agent_name,
    agent_channel:  $agent_channel,
    channel_config: $channel_config
  }')"

# ── Merge into existing SSM parameter ────────────────────────────────────────
EXISTING="$(aws ssm get-parameter \
  --name "/clawless/clients" \
  --region "${REGION}" \
  --query Parameter.Value \
  --output text 2>/dev/null || echo '{}')"

UPDATED="$(echo "$EXISTING" | jq \
  --arg slug "$SLUG" \
  --argjson entry "$CLIENT_JSON" \
  '. + {($slug): $entry}')"

aws ssm put-parameter \
  --name "/clawless/clients" \
  --type "String" \
  --value "$UPDATED" \
  --overwrite \
  --region "${REGION}"

hr
echo "Agent '${SLUG}' added to /clawless/clients."
echo "Run 'tofu apply' to provision."
hr
