#!/usr/bin/env bash
# resume.sh — Resume a paused agent by setting active=true in SSM.
# The lifecycle Lambda discovers the pause snapshot, restores the instance,
# and cleans up the snapshot automatically.
#
# Usage: ./scripts/resume.sh <client-slug> <agent-slug> [--region <region>]
# Example: ./scripts/resume.sh zalman wingmate

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TOFU_DIR="$REPO_ROOT/tofu"

hr() { printf '%*s\n' 72 '' | tr ' ' '-'; }
log() { echo "[resume] $*"; }

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

AGENT_PARAM="/clawless/clients/${CLIENT_SLUG}/${AGENT_SLUG}"
RESOURCE_SLUG="${CLIENT_SLUG}-${AGENT_SLUG}"

hr
log "Resuming agent: ${CLIENT_SLUG}/${AGENT_SLUG}"
log "  Region: $REGION"
hr

CURRENT=$(aws ssm get-parameter \
  --name "$AGENT_PARAM" \
  --with-decryption \
  --query 'Parameter.Value' \
  --output text --region "$REGION" 2>/dev/null || true)

if [[ -z "$CURRENT" ]]; then
  log "ERROR: agent '${CLIENT_SLUG}/${AGENT_SLUG}' not found in SSM" >&2; exit 1
fi
if [[ "$(echo "$CURRENT" | jq -r '.active != false')" == "true" ]]; then
  log "ERROR: agent ${CLIENT_SLUG}/${AGENT_SLUG} is already active" >&2; exit 1
fi

log "Updating ${AGENT_PARAM} (active=true)..."
UPDATED=$(echo "$CURRENT" | jq '.active = true')
aws ssm put-parameter \
  --name "$AGENT_PARAM" \
  --type String \
  --overwrite \
  --value "$UPDATED" \
  --region "$REGION" >/dev/null

log "Resume triggered. Waiting for SSM agent to reconnect..."
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
log "Agent ${CLIENT_SLUG}/${AGENT_SLUG} resumed."
hr
