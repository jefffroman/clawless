#!/usr/bin/env bash
# ssm-run.sh — Run a shell command on a clawless managed instance via SSM.
#
# Usage: ./scripts/ssm-run.sh <managed-instance-id> <command> [--region <region>]
#        ./scripts/ssm-run.sh --slug <client-slug> <command> [--region <region>]
#
# Examples:
#   ./scripts/ssm-run.sh mi-05ab4d8c74833604c "ls /var/lib/openclaw"
#   ./scripts/ssm-run.sh --slug test "cat /var/log/cloud-init-output.log"

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TOFU_DIR="$SCRIPT_DIR/../tofu"

REGION=""
INSTANCE_ID=""
SLUG=""
CMD=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region) REGION="$2"; shift 2 ;;
    --slug)   SLUG="$2"; shift 2 ;;
    -*) echo "Unknown flag: $1" >&2; exit 1 ;;
    *)
      if [[ -z "$INSTANCE_ID" && -z "$SLUG" ]]; then
        INSTANCE_ID="$1"
      elif [[ -n "$SLUG" && -z "$CMD" ]]; then
        CMD="$1"
      elif [[ -z "$CMD" ]]; then
        CMD="$1"
      fi
      shift
      ;;
  esac
done

if [[ -z "$REGION" ]]; then
  REGION=$(grep '^aws_region' "$TOFU_DIR/terraform.tfvars" 2>/dev/null \
    | awk -F'"' '{print $2}' || true)
  REGION="${REGION:-us-east-1}"
fi

# Resolve slug → instance ID
if [[ -n "$SLUG" && -z "$INSTANCE_ID" ]]; then
  INSTANCE_ID=$(aws ssm describe-instance-information \
    --region "$REGION" \
    --query "InstanceInformationList[?Name=='clawless-${SLUG}' && PingStatus=='Online'].InstanceId | [0]" \
    --output text)
  if [[ -z "$INSTANCE_ID" || "$INSTANCE_ID" == "None" ]]; then
    echo "ERROR: No online SSM instance found for slug '${SLUG}'" >&2
    exit 1
  fi
  echo "Resolved clawless-${SLUG} → ${INSTANCE_ID}"
fi

if [[ -z "$INSTANCE_ID" || -z "$CMD" ]]; then
  echo "Usage: $0 <instance-id|--slug <slug>> <command> [--region <region>]" >&2
  exit 1
fi

echo "Running on ${INSTANCE_ID}: ${CMD}"

CMD_ID=$(aws ssm send-command \
  --instance-ids "$INSTANCE_ID" \
  --document-name "AWS-RunShellScript" \
  --parameters "commands=[\"$CMD\"]" \
  --region "$REGION" \
  --query 'Command.CommandId' \
  --output text)

echo "CommandId: $CMD_ID"

for i in $(seq 1 60); do
  RESULT=$(aws ssm get-command-invocation \
    --command-id "$CMD_ID" \
    --instance-id "$INSTANCE_ID" \
    --region "$REGION" 2>&1) || { sleep 3; continue; }
  STATUS=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['Status'])" 2>/dev/null || echo "unknown")
  [[ "$STATUS" == "InProgress" || "$STATUS" == "Pending" || "$STATUS" == "unknown" ]] \
    || { echo "Status: $STATUS"
         echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['StandardOutputContent'])" 2>/dev/null
         ERR=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['StandardErrorContent'])" 2>/dev/null || true)
         [[ -n "$ERR" ]] && echo "STDERR: $ERR"
         break; }
  echo "  waiting... ($i/60)"
  sleep 5
done
