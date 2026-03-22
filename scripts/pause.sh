#!/usr/bin/env bash
# pause.sh — Pause a client by setting active=false in SSM.
# The lifecycle Lambda handles snapshot creation and instance destruction automatically.
#
# Usage: ./scripts/pause.sh <client-slug> [--region <region>]

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TOFU_DIR="$REPO_ROOT/tofu"

hr() { printf '%*s\n' 72 '' | tr ' ' '-'; }
log() { echo "[pause] $*"; }

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <client-slug> [--region <region>]" >&2
  exit 1
fi

SLUG="$1"; shift
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

hr
log "Pausing client: $SLUG"
log "  Region: $REGION"
hr

CURRENT=$(aws ssm get-parameter \
  --name /clawless/clients \
  --query 'Parameter.Value' \
  --output text --region "$REGION")

if [[ "$(echo "$CURRENT" | jq -r --arg s "$SLUG" '.[$s] // empty')" == "" ]]; then
  log "ERROR: client '$SLUG' not found in /clawless/clients" >&2; exit 1
fi
if [[ "$(echo "$CURRENT" | jq -r --arg s "$SLUG" '.[$s].active != false')" == "false" ]]; then
  log "ERROR: client $SLUG is already paused" >&2; exit 1
fi

# Clean cloud-init state on the instance before snapshotting so that on resume
# cloud-init treats the restored instance as a first boot and runs user-data.
INSTANCE_ID=$(aws ssm describe-instance-information \
  --region "$REGION" \
  --query "InstanceInformationList[?Name=='clawless-${SLUG}' && PingStatus=='Online'].InstanceId | [0]" \
  --output text 2>/dev/null || true)

if [[ -n "$INSTANCE_ID" && "$INSTANCE_ID" != "None" ]]; then
  log "Cleaning cloud-init state on $INSTANCE_ID before snapshot..."
  CMD_ID=$(aws ssm send-command \
    --instance-ids "$INSTANCE_ID" \
    --document-name "AWS-RunShellScript" \
    --parameters 'commands=["cloud-init clean --logs"]' \
    --region "$REGION" \
    --query 'Command.CommandId' \
    --output text)
  for i in $(seq 1 12); do
    STATUS=$(aws ssm get-command-invocation \
      --command-id "$CMD_ID" \
      --instance-id "$INSTANCE_ID" \
      --region "$REGION" \
      --query 'Status' --output text 2>/dev/null || echo "Pending")
    [[ "$STATUS" != "InProgress" && "$STATUS" != "Pending" ]] && break
    sleep 5
  done
  log "cloud-init clean status: $STATUS"
else
  log "WARNING: No online SSM instance found for $SLUG — skipping cloud-init clean."
fi

log "Updating /clawless/clients (active=false)..."
UPDATED=$(echo "$CURRENT" | jq --arg s "$SLUG" '.[$s].active = false')
aws ssm put-parameter \
  --name /clawless/clients \
  --type String \
  --overwrite \
  --value "$UPDATED" \
  --region "$REGION" >/dev/null

hr
log "Pause triggered. The lifecycle Lambda will snapshot and destroy the instance."
log "Resume with: ./scripts/resume.sh $SLUG"
hr
