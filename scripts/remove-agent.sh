#!/usr/bin/env bash
# remove-agent.sh — Remove an agent, triggering full resource teardown.
#
# Invokes the lifecycle Step Functions workflow which deletes the agent's
# SSM record and triggers the Lambda to destroy all AWS resources.
#
# Usage: ./scripts/remove-agent.sh <client-slug> <agent-slug> [--force] [--region <region>]
# Example: ./scripts/remove-agent.sh zalman wingmate

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TOFU_DIR="$REPO_ROOT/tofu"

hr() { printf '%*s\n' 72 '' | tr ' ' '-'; }
log() { echo "[remove] $*"; }

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <client-slug> <agent-slug> [--force] [--region <region>]" >&2
  exit 1
fi

CLIENT_SLUG="$1"; AGENT_SLUG="$2"; shift 2
REGION=""
FORCE=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --region) REGION="$2"; shift 2 ;;
    --force)  FORCE=true; shift ;;
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
log "Removing agent: ${CLIENT_SLUG}-${AGENT_SLUG}"
log "  Region: $REGION"
hr

# Check if agent SSM record exists
SSM_MISSING=false
if ! aws ssm get-parameter --name "$AGENT_PARAM" --region "$REGION" >/dev/null 2>&1; then
  log "WARNING: SSM parameter '${AGENT_PARAM}' not found — already deleted?"
  log "  The Fargate service and other AWS resources may still exist."
  log "  Proceeding with lifecycle invocation to ensure full cleanup."
  SSM_MISSING=true
fi

if [[ "$FORCE" == false ]]; then
  echo "This will permanently destroy all AWS resources for ${CLIENT_SLUG}-${AGENT_SLUG}."
  read -rp "Type the agent slug to confirm (${AGENT_SLUG}): " CONFIRM
  if [[ "$CONFIRM" != "$AGENT_SLUG" ]]; then
    log "Aborted." >&2; exit 1
  fi
fi

# Delete active flag and error state (the main SSM record is deleted by the
# Step Functions DeleteSSM state — deleting it here would cause SFN to fail
# with ParameterNotFound since DeleteSSM has no error handling).
aws ssm delete-parameter --name "${AGENT_PARAM}/active" --region "$REGION" 2>/dev/null || true
aws ssm delete-parameter --name "${AGENT_PARAM}/error" --region "$REGION" 2>/dev/null || true

log "Invoking Step Functions (lifecycle)..."
SFN_ARN=$(aws stepfunctions list-state-machines --region "$REGION" \
  --query 'stateMachines[?name==`clawless-lifecycle`].stateMachineArn | [0]' --output text)

# If the SSM record is already gone, use "Update" to skip the SFN DeleteSSM
# state (which would fail with ParameterNotFound). The Lambda detects removed
# agents by comparing tofu state vs SSM — the param just needs to be absent.
if [[ "$SSM_MISSING" == true ]]; then
  SFN_OP="Update"
else
  SFN_OP="Delete"
fi

SFN_INPUT=$(jq -cn \
  --arg name "$AGENT_PARAM" \
  --arg time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg op "$SFN_OP" \
  '{event_id: (now | tostring), time: $time, name: $name, operation: $op}')
aws stepfunctions start-execution \
  --state-machine-arn "$SFN_ARN" \
  --input "$SFN_INPUT" \
  --region "$REGION" >/dev/null
log "Step Functions invoked."

hr
log "Agent ${CLIENT_SLUG}-${AGENT_SLUG} removed. The lifecycle Lambda will destroy its resources."
hr
