#!/usr/bin/env bash
# Restore an agent whose instance was accidentally deleted.
#
# Verifies the agent's SSM entry still exists and that S3 backup data is
# available, then re-triggers the lifecycle Lambda to recreate the instance.
# Once the instance is online, restores the workspace from S3 backup via SSM.
#
# Normal wake (pause/resume) restores from the Lightsail snapshot, which has
# exact state. This script is for disaster recovery when the snapshot is gone.
#
# If the SSM entry is also gone, use add-agent.sh to re-register first.
#
# Usage: restore-agent.sh --slug <client/agent> [--region <region>] [--before <datetime>]
#
# Options:
#   --before  Restore workspace as it was before this timestamp (ISO 8601).
#             Uses S3 versioning to retrieve prior object versions.
#             Example: --before 2026-03-20T12:00:00Z
#             Without this flag, restores the latest (current) backup.
set -euo pipefail

REGION="us-east-1"
SLUG=""
BEFORE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --slug)   SLUG="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --before) BEFORE="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$SLUG" ]]; then
  echo "Usage: restore-agent.sh --slug <client/agent> [--region <region>] [--before <datetime>]" >&2
  exit 1
fi

CLIENT_SLUG="${SLUG%%/*}"
AGENT_SLUG="${SLUG##*/}"
RESOURCE_SLUG="${CLIENT_SLUG}-${AGENT_SLUG}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
BACKUP_BUCKET="clawless-backups-${ACCOUNT_ID}"
BACKUP_PREFIX="agents/${SLUG}/workspace/"

hr()  { echo "────────────────────────────────────────────────────────"; }

# ── Verify SSM entry exists ──────────────────────────────────────────────────
hr
echo "Checking SSM entry for ${SLUG}..."
SSM_PATH="/clawless/clients/${CLIENT_SLUG}/${AGENT_SLUG}"
if aws ssm get-parameter --name "$SSM_PATH" --region "$REGION" >/dev/null 2>&1; then
  SSM_VALUE="$(aws ssm get-parameter --name "$SSM_PATH" --region "$REGION" --with-decryption --query 'Parameter.Value' --output text)"
  echo "  Found: ${SSM_VALUE}"

  ACTIVE="$(echo "$SSM_VALUE" | jq -r '.active // true')"
  if [[ "$ACTIVE" == "false" ]]; then
    echo "  WARNING: Agent is paused (active: false)."
    echo "  The lifecycle Lambda will not create an instance for a paused agent."
    echo "  Use resume.sh instead, or set active: true first."
    exit 1
  fi
else
  echo "  NOT FOUND: ${SSM_PATH}"
  echo "  Re-register the agent with add-agent.sh first, then re-run this script."
  exit 1
fi

# ── Verify backup data exists ────────────────────────────────────────────────
hr
echo "Checking S3 backup at s3://${BACKUP_BUCKET}/${BACKUP_PREFIX}..."
OBJECT_COUNT="$(aws s3 ls "s3://${BACKUP_BUCKET}/${BACKUP_PREFIX}" --recursive --region "$REGION" 2>/dev/null | wc -l | tr -d ' ')"

if [[ "$OBJECT_COUNT" -gt 0 ]]; then
  echo "  Found ${OBJECT_COUNT} objects in backup."
else
  echo "  WARNING: No backup data found."
  echo "  The instance will be created with a fresh workspace (no history)."
  read -rp "  Continue anyway? [y/N]: " CONFIRM
  [[ "${CONFIRM,,}" == "y" ]] || exit 0
fi

# ── Check if instance already exists ─────────────────────────────────────────
hr
echo "Checking if instance clawless-${RESOURCE_SLUG} exists..."
if aws lightsail get-instance --instance-name "clawless-${RESOURCE_SLUG}" --region "$REGION" >/dev/null 2>&1; then
  echo "  Instance already exists! Nothing to restore."
  echo "  If the instance is broken, delete it first, then re-run this script."
  exit 0
fi
echo "  Instance not found — will recreate."

# ── Trigger lifecycle Lambda ─────────────────────────────────────────────────
hr
echo "Triggering lifecycle Lambda to recreate instance..."

# Touch the agent's SSM entry to trigger EventBridge → Lambda.
aws ssm put-parameter \
  --name "$SSM_PATH" \
  --value "$SSM_VALUE" \
  --type String \
  --overwrite \
  --region "$REGION" >/dev/null

echo "  SSM parameter touched — EventBridge will invoke the lifecycle Lambda."
echo "  The Lambda will create the instance from the golden snapshot."

# ── Wait for instance to come online ─────────────────────────────────────────
hr
echo "Waiting for instance to register with SSM..."
ROLE_NAME="clawless-${RESOURCE_SLUG}-ssm"
MI_ID=""
for i in $(seq 1 40); do
  MI_ID="$(aws ssm describe-instance-information \
    --filters "Key=IamRole,Values=${ROLE_NAME}" \
    --region "$REGION" \
    --query 'InstanceInformationList[?PingStatus==`Online`].InstanceId | [0]' \
    --output text 2>/dev/null)"
  if [[ -n "$MI_ID" && "$MI_ID" != "None" ]]; then
    echo "  Instance online: ${MI_ID}"
    break
  fi
  printf "  Waiting... (%d/40)\r" "$i"
  sleep 15
done

if [[ -z "$MI_ID" || "$MI_ID" == "None" ]]; then
  echo "  Timed out waiting for instance. Check Lambda logs:"
  echo "    aws logs tail /aws/lambda/clawless-lifecycle --follow --region ${REGION}"
  exit 1
fi

# ── Restore workspace from S3 ───────────────────────────────────────────────
if [[ "$OBJECT_COUNT" -gt 0 ]]; then
  hr
  if [[ -z "$BEFORE" ]]; then
    echo "Restoring workspace from latest S3 backup..."
    aws ssm send-command \
      --instance-ids "$MI_ID" \
      --document-name AWS-RunShellScript \
      --region "$REGION" \
      --parameters "commands=[
        \"aws s3 sync s3://${BACKUP_BUCKET}/${BACKUP_PREFIX} /home/agent/ --no-progress\",
        \"chown -R agent:agent /home/agent\",
        \"echo 'Workspace restored from latest S3 backup'\"
      ]" \
      --comment "restore-agent: S3 workspace recovery (latest)" \
      --output text --query 'Command.CommandId'
  else
    echo "Restoring workspace from S3 backup before ${BEFORE}..."
    # Build a restore script that uses S3 versioning to download each object
    # as it was before the given timestamp. Runs on the instance via SSM.
    RESTORE_SCRIPT=$(cat <<'INNEREOF'
#!/bin/bash
set -euo pipefail
BUCKET="$1"
PREFIX="$2"
BEFORE="$3"
DEST="/home/agent"
REGION="$4"

echo "Listing object versions before ${BEFORE}..."
aws s3api list-object-versions \
  --bucket "$BUCKET" \
  --prefix "$PREFIX" \
  --region "$REGION" \
  --query "Versions[?LastModified<\`${BEFORE}\`]" \
  --output json \
| python3 -c "
import json, sys
from collections import defaultdict

versions = json.load(sys.stdin) or []
# Group by key, pick the most recent version before the cutoff
by_key = defaultdict(list)
for v in versions:
    by_key[v['Key']].append(v)

for key, vs in by_key.items():
    vs.sort(key=lambda x: x['LastModified'], reverse=True)
    latest = vs[0]
    rel = key[len('$PREFIX'):]
    print(json.dumps({'key': key, 'version': latest['VersionId'], 'rel': rel}))
" | while IFS= read -r line; do
  KEY="$(echo "$line" | jq -r .key)"
  VID="$(echo "$line" | jq -r .version)"
  REL="$(echo "$line" | jq -r .rel)"
  DEST_PATH="${DEST}/${REL}"
  mkdir -p "$(dirname "$DEST_PATH")"
  aws s3api get-object \
    --bucket "$BUCKET" \
    --key "$KEY" \
    --version-id "$VID" \
    --region "$REGION" \
    "$DEST_PATH" >/dev/null
done

chown -R agent:agent "$DEST"
echo "Workspace restored from S3 versions before ${BEFORE}"
INNEREOF
    )

    aws ssm send-command \
      --instance-ids "$MI_ID" \
      --document-name AWS-RunShellScript \
      --region "$REGION" \
      --parameters "commands=[\"bash -c 'cat > /tmp/restore-versioned.sh << '\\''SCRIPT'\\''\\n${RESTORE_SCRIPT}\\nSCRIPT\\nbash /tmp/restore-versioned.sh ${BACKUP_BUCKET} ${BACKUP_PREFIX} ${BEFORE} ${REGION}'\"]" \
      --comment "restore-agent: S3 versioned workspace recovery (before ${BEFORE})" \
      --output text --query 'Command.CommandId'
  fi

  echo "  S3 restore command sent. Workspace will be synced shortly."
fi

hr
echo "Restore complete."
echo "  Instance: clawless-${RESOURCE_SLUG}"
echo "  SSM ID:   ${MI_ID}"
echo "  Connect:  ./scripts/ssm-run.sh --slug ${SLUG} 'systemctl --user status openclaw-gateway'"
