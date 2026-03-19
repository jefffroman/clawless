#!/usr/bin/env bash
# resume.sh — Resume a paused client by setting active=true and running tofu apply.
# Tofu discovers the per-client snapshot (clawless-{slug}-snap) automatically
# and restores from it. No Ansible is run — the instance boots fully configured.
#
# Usage: ./scripts/resume.sh <client-slug> [--region <region>]

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TOFU_DIR="$REPO_ROOT/tofu"

hr() { printf '%*s\n' 72 '' | tr ' ' '-'; }
log() { echo "[resume] $*"; }

# ── Args ──────────────────────────────────────────────────────────────────────

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
log "Resuming client: $SLUG"
log "  Region: $REGION"
hr

# ── Verify client exists and is paused ───────────────────────────────────────

CLIENTS=$(aws ssm get-parameter \
  --name /clawless/clients \
  --query 'Parameter.Value' \
  --output text --region "$REGION")

if [[ "$(echo "$CLIENTS" | jq -r --arg s "$SLUG" '.[$s] // empty')" == "" ]]; then
  log "ERROR: client '$SLUG' not found in SSM /clawless/clients" >&2
  exit 1
fi

ACTIVE=$(echo "$CLIENTS" | jq -r --arg s "$SLUG" '.[$s].active != false')
if [[ "$ACTIVE" == "true" ]]; then
  log "ERROR: client $SLUG is already active" >&2
  exit 1
fi

# ── Update SSM: set active=true ───────────────────────────────────────────────

log "Updating SSM /clawless/clients (active=true)..."
UPDATED=$(echo "$CLIENTS" | jq --arg slug "$SLUG" '.[$slug].active = true')

aws ssm put-parameter \
  --name /clawless/clients \
  --type String \
  --overwrite \
  --value "$UPDATED" \
  --region "$REGION" >/dev/null

# ── tofu apply — discovers snapshot, restores instance, sets firewall ─────────

log "Running tofu apply (snapshot discovered automatically, no Ansible)..."
cd "$TOFU_DIR"
tofu apply -auto-approve

# ── Delete snapshot now that instance is running ──────────────────────────────

log "Deleting snapshot clawless-$SLUG-snap..."
aws lightsail delete-instance-snapshot \
  --instance-snapshot-name "clawless-$SLUG-snap" \
  --region "$REGION" >/dev/null 2>&1 || log "WARNING: snapshot delete failed — clean up manually if needed."

# ── Wait for SSM agent to reconnect ──────────────────────────────────────────

log "Waiting for SSM agent to reconnect..."
INSTANCE_ID=""
for i in $(seq 1 20); do
  INSTANCE_ID=$(aws ssm describe-instance-information \
    --filters "Key=IamRole,Values=clawless-${SLUG}-ssm" \
    --query 'InstanceInformationList[0].InstanceId' \
    --output text --region "$REGION" 2>/dev/null || true)
  [[ -n "$INSTANCE_ID" && "$INSTANCE_ID" != "None" ]] && break
  sleep 15
done

if [[ -z "$INSTANCE_ID" || "$INSTANCE_ID" == "None" ]]; then
  log "WARNING: SSM agent did not reconnect within 5 minutes — may still be starting."
else
  log "SSM agent reconnected ($INSTANCE_ID)."
fi

hr
log "Client $SLUG resumed. Snapshot discovered and restored automatically."
hr
