#!/usr/bin/env bash
# pause-agent.sh — Pause an agent by setting its /active parameter to "false".
# The lifecycle Lambda handles snapshot creation and instance destruction automatically.
#
# Usage: ./scripts/pause-agent.sh <client-slug> <agent-slug> [--region <region>]
# Example: ./scripts/pause-agent.sh zalman wingmate

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TOFU_DIR="$REPO_ROOT/tofu"

hr() { printf '%*s\n' 72 '' | tr ' ' '-'; }
log() { echo "[pause] $*"; }

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

hr
log "Pausing agent: ${CLIENT_SLUG}/${AGENT_SLUG}"
log "  Region: $REGION"
hr

CURRENT=$(aws ssm get-parameter \
  --name "$ACTIVE_PARAM" \
  --query 'Parameter.Value' \
  --output text --region "$REGION" 2>/dev/null || true)

if [[ -z "$CURRENT" ]]; then
  log "ERROR: agent '${CLIENT_SLUG}/${AGENT_SLUG}' not found in SSM" >&2; exit 1
fi
if [[ "$CURRENT" == "false" ]]; then
  log "ERROR: agent ${CLIENT_SLUG}/${AGENT_SLUG} is already paused" >&2; exit 1
fi

log "Updating ${ACTIVE_PARAM} → false..."
aws ssm put-parameter \
  --name "$ACTIVE_PARAM" \
  --type String \
  --overwrite \
  --value "false" \
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

hr
log "Pause triggered. The lifecycle Lambda will snapshot and destroy the instance."
log "Resume with: ./scripts/resume-agent.sh ${CLIENT_SLUG} ${AGENT_SLUG}"
hr
