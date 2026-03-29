#!/usr/bin/env bash
# wake-agent.sh — Wake a sleeping agent by setting its /active parameter to "true".
# The lifecycle Lambda discovers the sleep snapshot, restores the instance,
# and cleans up the snapshot automatically.
#
# Usage: ./scripts/wake-agent.sh <client-slug> <agent-slug> [--region <region>]
# Example: ./scripts/wake-agent.sh zalman wingmate

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TOFU_DIR="$REPO_ROOT/tofu"

hr() { printf '%*s\n' 72 '' | tr ' ' '-'; }
log() { echo "[wake] $*"; }

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <client-slug> <agent-slug> [--region <region>]" >&2
  exit 1
fi

CLIENT_SLUG="$1"; AGENT_SLUG="$2"; shift 2
REGION=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --region) REGION="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$REGION" ]]; then
  REGION=$(grep '^aws_region' "$TOFU_DIR/terraform.tfvars" 2>/dev/null \
    | awk -F'"' '{print $2}' || true)
  REGION="${REGION:-us-east-1}"
fi

ACTIVE_PARAM="/clawless/clients/${CLIENT_SLUG}/${AGENT_SLUG}/active"
AGENT_PARAM="/clawless/clients/${CLIENT_SLUG}/${AGENT_SLUG}"
RESOURCE_SLUG="${CLIENT_SLUG}-${AGENT_SLUG}"

hr
log "Waking agent: ${CLIENT_SLUG}/${AGENT_SLUG}"
log "  Region: $REGION"
hr

CURRENT=$(aws ssm get-parameter \
  --name "$ACTIVE_PARAM" \
  --query 'Parameter.Value' \
  --output text --region "$REGION" 2>/dev/null || true)

if [[ -z "$CURRENT" ]]; then
  log "ERROR: agent '${CLIENT_SLUG}/${AGENT_SLUG}' not found in SSM" >&2; exit 1
fi
if [[ "$CURRENT" == "true" ]]; then
  log "ERROR: agent ${CLIENT_SLUG}/${AGENT_SLUG} is already active" >&2; exit 1
fi

log "Updating ${ACTIVE_PARAM} → true..."
aws ssm put-parameter \
  --name "$ACTIVE_PARAM" \
  --type String \
  --overwrite \
  --value "true" \
  --region "$REGION" >/dev/null

log "Invoking Step Functions (lifecycle)..."
SFN_ARN=$(aws stepfunctions list-state-machines --region "$REGION" \
  --query 'stateMachines[?name==`clawless-lifecycle`].stateMachineArn | [0]' --output text)
SFN_INPUT=$(jq -cn \
  --arg name "$AGENT_PARAM" \
  --arg time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  '{event_id: (now | tostring), time: $time, name: $name, operation: "Update"}')
aws stepfunctions start-execution \
  --state-machine-arn "$SFN_ARN" \
  --input "$SFN_INPUT" \
  --region "$REGION" >/dev/null
log "Step Functions invoked."

log "Waiting for SSM agent to reconnect..."
INSTANCE_ID=""
for i in $(seq 1 24); do
  INSTANCE_ID=$(aws ssm describe-instance-information \
    --filters "Key=IamRole,Values=clawless-${RESOURCE_SLUG}-ssm" \
    --query "InstanceInformationList[?PingStatus=='Online'].InstanceId | [0]" \
    --output text --region "$REGION" 2>/dev/null || true)
  [[ -n "$INSTANCE_ID" && "$INSTANCE_ID" != "None" ]] && break
  sleep 15
done

if [[ -z "$INSTANCE_ID" || "$INSTANCE_ID" == "None" ]]; then
  log "WARNING: SSM agent did not reconnect within 6 minutes — may still be starting."
else
  log "SSM agent reconnected ($INSTANCE_ID)."
fi

hr
log "Agent ${CLIENT_SLUG}/${AGENT_SLUG} is waking up."
hr
