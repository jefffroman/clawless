#!/usr/bin/env bash
# remove-agent.sh — Remove an agent from SSM, triggering full resource teardown.
#
# Deletes the agent's SSM record. The lifecycle Lambda fires automatically
# via EventBridge and destroys all AWS resources for the agent.
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

# Verify agent exists
if ! aws ssm get-parameter --name "$AGENT_PARAM" --region "$REGION" >/dev/null 2>&1; then
  log "ERROR: agent '${CLIENT_SLUG}-${AGENT_SLUG}' not found in SSM" >&2; exit 1
fi

if [[ "$FORCE" == false ]]; then
  echo "This will permanently destroy all AWS resources for ${CLIENT_SLUG}-${AGENT_SLUG}."
  read -rp "Type the agent slug to confirm (${AGENT_SLUG}): " CONFIRM
  if [[ "$CONFIRM" != "$AGENT_SLUG" ]]; then
    log "Aborted." >&2; exit 1
  fi
fi

# Delete agent record
log "Deleting ${AGENT_PARAM}..."
aws ssm delete-parameter --name "$AGENT_PARAM" --region "$REGION"

hr
log "Agent ${CLIENT_SLUG}-${AGENT_SLUG} removed. The lifecycle Lambda will destroy its resources."
hr
