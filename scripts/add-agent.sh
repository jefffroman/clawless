#!/usr/bin/env bash
# Register a new agent in the /clawless/clients SSM hierarchy.
#
# SSM structure:
#   /clawless/clients/{client_slug}/{agent_slug} → {agent config incl. client_name}
#
# Client slug uniqueness is enforced by the storefront (clawless-platform).
#
# Usage: add-agent.sh [--region <region>]
set -euo pipefail

REGION="us-east-1"
VERBOSE="false"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --region)  REGION="$2"; shift 2 ;;
    --verbose) VERBOSE="true"; shift 1 ;;
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
ask CHANNEL "Channel (telegram / discord / slack)" "telegram"
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
    echo "Error: unsupported channel '$CHANNEL'. Supported: telegram, discord, slack" >&2
    exit 1
    ;;
esac

# ── Write agent record ────────────────────────────────────────────────────────
AGENT_VALUE="$(jq -cn \
  --arg client_name    "$CLIENT_NAME" \
  --arg agent_name     "$AGENT_NAME" \
  --arg agent_channel  "$CHANNEL" \
  --argjson channel_config "$CHANNEL_CONFIG" \
  '{
    client_name:    $client_name,
    agent_name:     $agent_name,
    agent_channel:  $agent_channel,
    channel_config: $channel_config
  }')"

AGENT_PARAM="/clawless/clients/${CLIENT_SLUG}/${AGENT_SLUG}"

# Write the verbose flag BEFORE invoking the lifecycle SFN so it's present
# when the Fargate task boots and entrypoint.sh reads it. Writing after the
# SFN call is a race: tofu-apply → ECS service → task boot can outrun the
# local put-parameter on a warm Lambda.
if [[ "$VERBOSE" == "true" ]]; then
  echo "Setting verbose flag for this agent..."
  aws ssm put-parameter \
    --name "${AGENT_PARAM}/verbose" \
    --type "String" \
    --value "true" \
    --overwrite \
    --region "${REGION}" >/dev/null
fi

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

# Active flag lives in its own parameter so agents can self-pause via a
# tightly scoped IAM policy (ssm:PutParameter on this path only).
aws ssm put-parameter \
  --name "/clawless/clients/${CLIENT_SLUG}/${AGENT_SLUG}/active" \
  --type "String" \
  --value "true" \
  --overwrite \
  --region "${REGION}"

hr
echo "Agent '${AGENT_NAME}' registered at ${AGENT_PARAM}."
echo "Resource slug: ${CLIENT_SLUG}-${AGENT_SLUG}"
hr
