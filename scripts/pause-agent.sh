#!/usr/bin/env bash
# pause.sh — Pause an agent by setting active=false in SSM.
# The lifecycle Lambda handles snapshot creation and instance destruction automatically.
#
# Usage: ./scripts/pause.sh <client-slug> <agent-slug> [--region <region>]
# Example: ./scripts/pause.sh zalman wingmate

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

AGENT_PARAM="/clawless/clients/${CLIENT_SLUG}/${AGENT_SLUG}"

hr
log "Pausing agent: ${CLIENT_SLUG}/${AGENT_SLUG}"
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
if [[ "$(echo "$CURRENT" | jq -r '.active != false')" == "false" ]]; then
  log "ERROR: agent ${CLIENT_SLUG}/${AGENT_SLUG} is already paused" >&2; exit 1
fi

UPDATED=$(echo "$CURRENT" | jq -c '.active = false')

log "Invoking Step Functions (SSM write + lifecycle)..."
SFN_ARN=$(aws stepfunctions list-state-machines --region "$REGION" \
  --query 'stateMachines[?name==`clawless-lifecycle`].stateMachineArn | [0]' --output text)
SFN_INPUT=$(jq -cn \
  --arg name "$AGENT_PARAM" \
  --arg time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg val  "$UPDATED" \
  '{event_id: (now | tostring), time: $time, name: $name, operation: "Update", ssm_value: $val}')
aws stepfunctions start-execution \
  --state-machine-arn "$SFN_ARN" \
  --input "$SFN_INPUT" \
  --region "$REGION" >/dev/null
log "Step Functions invoked."

hr
log "Pause triggered. The lifecycle Lambda will snapshot and destroy the instance."
log "Resume with: ./scripts/resume-agent.sh ${CLIENT_SLUG} ${AGENT_SLUG}"
hr
