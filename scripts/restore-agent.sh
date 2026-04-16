#!/usr/bin/env bash
# restore-agent.sh — Roll an agent's workspace back to a prior point in time.
#
# Uses S3 object versioning on the shared backup bucket to restore
# agents/{slug}/workspace/* to how it looked before a given timestamp, then
# forces a new Fargate deployment so the running task picks up the rolled-back
# state on its next sync_down.
#
# Usage: restore-agent.sh --slug <client/agent> --before <datetime> [--region <region>]
# Example: restore-agent.sh --slug zalman/wingmate --before 2026-04-10T12:00:00Z
set -euo pipefail

REGION="us-east-1"
SLUG=""
BEFORE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --slug)   SLUG="$2"; shift 2 ;;
    --before) BEFORE="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$SLUG" || -z "$BEFORE" ]]; then
  echo "Usage: restore-agent.sh --slug <client/agent> --before <datetime> [--region <region>]" >&2
  exit 1
fi

CLIENT_SLUG="${SLUG%%/*}"
AGENT_SLUG="${SLUG##*/}"
RESOURCE_SLUG="${CLIENT_SLUG}-${AGENT_SLUG}"
SERVICE_NAME="clawless-${RESOURCE_SLUG}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
BACKUP_BUCKET="clawless-backups-${ACCOUNT_ID}"
PREFIX="agents/${SLUG}/workspace/"

hr() { echo "────────────────────────────────────────────────────────"; }

hr
echo "Restoring ${SLUG} to state before ${BEFORE}"
echo "  Bucket : s3://${BACKUP_BUCKET}/${PREFIX}"
echo "  Region : ${REGION}"
hr

# ── Scale the service to 0 so the running task doesn't race the restore ──────
# Reading files from S3 while the task is writing new ones is incoherent;
# we stop the task, restore, then scale back.
WAS_RUNNING=$(aws ecs describe-services \
  --cluster clawless --services "$SERVICE_NAME" --region "$REGION" \
  --query 'services[0].desiredCount' --output text 2>/dev/null || echo 0)

if [[ "$WAS_RUNNING" == "1" ]]; then
  echo "Scaling ${SERVICE_NAME} to 0 (task will sync-up on SIGTERM)..."
  aws ecs update-service \
    --cluster clawless --service "$SERVICE_NAME" \
    --desired-count 0 --region "$REGION" >/dev/null
  echo "Waiting for task to drain..."
  aws ecs wait services-stable \
    --cluster clawless --services "$SERVICE_NAME" --region "$REGION"
fi

# ── Restore each object to its most recent version before $BEFORE ────────────
hr
echo "Listing object versions before ${BEFORE}..."
TMP_VERSIONS="$(mktemp)"
trap 'rm -f "$TMP_VERSIONS"' EXIT

aws s3api list-object-versions \
  --bucket "$BACKUP_BUCKET" \
  --prefix "$PREFIX" \
  --region "$REGION" \
  --output json > "$TMP_VERSIONS"

# For each key, pick the newest version older than the cutoff. Python keeps
# this readable and handles the group-by without jq gymnastics.
RESTORE_PLAN="$(python3 - "$TMP_VERSIONS" "$BEFORE" <<'PY'
import json, sys
versions_path, cutoff = sys.argv[1], sys.argv[2]
data = json.load(open(versions_path))
by_key = {}
for v in data.get("Versions", []):
    if v["LastModified"] >= cutoff:
        continue
    cur = by_key.get(v["Key"])
    if cur is None or v["LastModified"] > cur["LastModified"]:
        by_key[v["Key"]] = v
for key, v in sorted(by_key.items()):
    print(f"{v['VersionId']}\t{key}")
PY
)"

if [[ -z "$RESTORE_PLAN" ]]; then
  echo "No object versions found before ${BEFORE}." >&2
  exit 1
fi

COUNT=$(echo "$RESTORE_PLAN" | wc -l | tr -d ' ')
echo "Will restore ${COUNT} object(s)."

# Copy each prior version onto the current key (creates a new current version).
while IFS=$'\t' read -r VID KEY; do
  aws s3api copy-object \
    --bucket "$BACKUP_BUCKET" \
    --key "$KEY" \
    --copy-source "${BACKUP_BUCKET}/${KEY}?versionId=${VID}" \
    --region "$REGION" >/dev/null
done <<< "$RESTORE_PLAN"

echo "Restore complete."

# ── Bring the service back up ────────────────────────────────────────────────
if [[ "$WAS_RUNNING" == "1" ]]; then
  hr
  echo "Scaling ${SERVICE_NAME} back to 1..."
  aws ecs update-service \
    --cluster clawless --service "$SERVICE_NAME" \
    --desired-count 1 --force-new-deployment --region "$REGION" >/dev/null
  echo "Service restarted. New task will sync the restored workspace on boot."
fi

hr
echo "Done. If the agent is sleeping, wake it with:"
echo "  ./scripts/wake-agent.sh ${CLIENT_SLUG} ${AGENT_SLUG}"
hr
