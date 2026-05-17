#!/usr/bin/env bash
# restore-agent.sh — Roll an agent's workspace back to a prior point in time.
#
# Single-archive model: an agent's whole workspace is one versioned S3 object
# (agents/{slug}/workspace.tar.zst). Point-in-time recovery = pick a prior
# S3 *version* of that one key and copy it onto the current version, then
# force a new Fargate deployment so the running task extracts the rolled-back
# archive on its next boot.
#
# Usage:
#   restore-agent.sh --slug <client/agent> --list [--region <region>]
#   restore-agent.sh --slug <client/agent> --before <datetime> [--region <region>]
#
#   --list    Print the version history of the workspace archive and exit
#             (no service changes). Use it to pick a --before cutoff.
#   --before  Restore the newest version older than this ISO-8601 instant,
#             e.g. 2026-04-10T12:00:00Z (must match the LastModified format
#             shown by --list).
set -euo pipefail

REGION="us-east-1"
SLUG=""
BEFORE=""
LIST=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --slug)   SLUG="$2"; shift 2 ;;
    --before) BEFORE="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --list)   LIST=1; shift ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

usage() {
  echo "Usage:" >&2
  echo "  restore-agent.sh --slug <client/agent> --list [--region <region>]" >&2
  echo "  restore-agent.sh --slug <client/agent> --before <datetime> [--region <region>]" >&2
  exit 1
}

if [[ -z "$SLUG" ]]; then usage; fi
if [[ "$LIST" -eq 0 && -z "$BEFORE" ]]; then usage; fi

CLIENT_SLUG="${SLUG%%/*}"
AGENT_SLUG="${SLUG##*/}"
RESOURCE_SLUG="${CLIENT_SLUG}-${AGENT_SLUG}"
SERVICE_NAME="clawless-${RESOURCE_SLUG}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
BACKUP_BUCKET="clawless-backups-${ACCOUNT_ID}"
OBJ_KEY="agents/${SLUG}/workspace.tar.zst"

hr() { echo "────────────────────────────────────────────────────────"; }

# ── --list: print version history of the single archive object ───────────────
if [[ "$LIST" -eq 1 ]]; then
  hr
  echo "Version history of s3://${BACKUP_BUCKET}/${OBJ_KEY}"
  hr
  aws s3api list-object-versions \
    --bucket "$BACKUP_BUCKET" \
    --prefix "$OBJ_KEY" \
    --region "$REGION" \
    --output json \
  | python3 - "$OBJ_KEY" <<'PY'
import json, sys
key = sys.argv[1]
data = json.load(sys.stdin)
vs = [v for v in data.get("Versions", []) if v["Key"] == key]
vs.sort(key=lambda v: v["LastModified"], reverse=True)
if not vs:
    print("(no versions found)")
else:
    print(f"{'LastModified':<26} {'Size':>12}  {'Latest':<6} VersionId")
    for v in vs:
        print(f"{v['LastModified']:<26} {v['Size']:>12}  "
              f"{'yes' if v.get('IsLatest') else 'no':<6} {v['VersionId']}")
PY
  hr
  echo "Restore one with:"
  echo "  ./scripts/restore-agent.sh --slug ${SLUG} --before <LastModified>"
  hr
  exit 0
fi

hr
echo "Restoring ${SLUG} to state before ${BEFORE}"
echo "  Object : s3://${BACKUP_BUCKET}/${OBJ_KEY}"
echo "  Region : ${REGION}"
hr

# ── Scale the service to 0 so the running task doesn't race the restore ──────
# Reading the archive while the task is mid-snapshot is incoherent; we stop
# the task, restore, then scale back.
WAS_RUNNING=$(aws ecs describe-services \
  --cluster clawless --services "$SERVICE_NAME" --region "$REGION" \
  --query 'services[0].desiredCount' --output text 2>/dev/null || echo 0)

if [[ "$WAS_RUNNING" == "1" ]]; then
  echo "Scaling ${SERVICE_NAME} to 0 (task will snapshot on SIGTERM)..."
  aws ecs update-service \
    --cluster clawless --service "$SERVICE_NAME" \
    --desired-count 0 --region "$REGION" >/dev/null
  echo "Waiting for task to drain..."
  aws ecs wait services-stable \
    --cluster clawless --services "$SERVICE_NAME" --region "$REGION"
fi

# ── Select the newest archive version older than the cutoff ──────────────────
hr
echo "Selecting newest version of workspace.tar.zst before ${BEFORE}..."
RESTORE_VID="$(aws s3api list-object-versions \
  --bucket "$BACKUP_BUCKET" \
  --prefix "$OBJ_KEY" \
  --region "$REGION" \
  --output json \
| python3 - "$OBJ_KEY" "$BEFORE" <<'PY'
import json, sys
key, cutoff = sys.argv[1], sys.argv[2]
data = json.load(sys.stdin)
cands = [v for v in data.get("Versions", [])
         if v["Key"] == key and v["LastModified"] < cutoff]
cands.sort(key=lambda v: v["LastModified"], reverse=True)
print(cands[0]["VersionId"] if cands else "")
PY
)"

if [[ -z "$RESTORE_VID" ]]; then
  echo "No version of ${OBJ_KEY} found before ${BEFORE}." >&2
  echo "List available versions with: ./scripts/restore-agent.sh --slug ${SLUG} --list" >&2
  exit 1
fi

echo "Restoring version ${RESTORE_VID} onto the current object."
aws s3api copy-object \
  --bucket "$BACKUP_BUCKET" \
  --key "$OBJ_KEY" \
  --copy-source "${BACKUP_BUCKET}/${OBJ_KEY}?versionId=${RESTORE_VID}" \
  --region "$REGION" >/dev/null

echo "Restore complete."

# ── Bring the service back up ────────────────────────────────────────────────
if [[ "$WAS_RUNNING" == "1" ]]; then
  hr
  echo "Scaling ${SERVICE_NAME} back to 1..."
  aws ecs update-service \
    --cluster clawless --service "$SERVICE_NAME" \
    --desired-count 1 --force-new-deployment --region "$REGION" >/dev/null
  echo "Service restarted. New task will extract the restored archive on boot."
fi

hr
echo "Done. If the agent is sleeping, wake it with:"
echo "  ./scripts/wake-agent.sh ${CLIENT_SLUG} ${AGENT_SLUG}"
hr
