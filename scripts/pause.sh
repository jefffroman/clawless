#!/usr/bin/env bash
# pause.sh — Snapshot a client's running instance, then destroy it.
# The snapshot is stored under the fixed name clawless-{slug}-snap so that
# tofu automatically discovers it on the next apply (no SSM tracking needed).
#
# Usage: ./scripts/pause.sh <client-slug> [--region <region>]
#
# After this script completes, billing switches from instance to snapshot
# (~$0.05/GB actual used data). Run resume.sh to bring the client back.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TOFU_DIR="$REPO_ROOT/tofu"

hr() { printf '%*s\n' 72 '' | tr ' ' '-'; }
log() { echo "[pause] $*"; }

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

INSTANCE_NAME="clawless-$SLUG"
SNAPSHOT_NAME="clawless-$SLUG-snap"

hr
log "Pausing client: $SLUG"
log "  Instance:  $INSTANCE_NAME"
log "  Snapshot:  $SNAPSHOT_NAME"
log "  Region:    $REGION"
hr

# ── Verify instance is running ────────────────────────────────────────────────

STATE=$(aws lightsail get-instance \
  --instance-name "$INSTANCE_NAME" \
  --query 'instance.state.name' \
  --output text --region "$REGION" 2>/dev/null || true)

if [[ -z "$STATE" ]]; then
  log "ERROR: instance $INSTANCE_NAME not found in $REGION" >&2
  exit 1
fi
if [[ "$STATE" != "running" ]]; then
  log "ERROR: instance is in state '$STATE', expected 'running'" >&2
  exit 1
fi

log "Taking snapshot..."
aws lightsail create-instance-snapshot \
  --instance-name "$INSTANCE_NAME" \
  --instance-snapshot-name "$SNAPSHOT_NAME" \
  --region "$REGION" >/dev/null

log "Waiting for snapshot to be available..."
until aws lightsail get-instance-snapshot \
    --instance-snapshot-name "$SNAPSHOT_NAME" \
    --query 'instanceSnapshot.state' \
    --output text --region "$REGION" 2>/dev/null | grep -q available; do
  sleep 10
done
log "Snapshot available."

# ── Update SSM: set active=false ──────────────────────────────────────────────

log "Updating SSM /clawless/clients (active=false)..."
CURRENT=$(aws ssm get-parameter \
  --name /clawless/clients \
  --query 'Parameter.Value' \
  --output text --region "$REGION")

UPDATED=$(echo "$CURRENT" | jq --arg slug "$SLUG" '.[$slug].active = false')

aws ssm put-parameter \
  --name /clawless/clients \
  --type String \
  --overwrite \
  --value "$UPDATED" \
  --region "$REGION" >/dev/null

# ── tofu apply to destroy instance ────────────────────────────────────────────

log "Running tofu apply to destroy instance..."
cd "$TOFU_DIR"
tofu apply -auto-approve

hr
log "Client $SLUG paused. Instance destroyed; snapshot $SNAPSHOT_NAME retained."
log "Resume with: ./scripts/resume.sh $SLUG"
hr
