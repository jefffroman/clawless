#!/usr/bin/env bash
# Manually invoke the lifecycle Lambda — useful when SSM is already up to date
# but the Lambda needs to re-run (e.g. after a code fix or failed invocation).
set -euo pipefail

REGION="us-east-1"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --region) REGION="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

RESPONSE_FILE="$(mktemp /tmp/clawless-lifecycle-response-XXXXXX.json)"
trap 'rm -f "$RESPONSE_FILE"' EXIT

aws lambda invoke \
  --function-name clawless-lifecycle \
  --payload '{"source":"manual"}' \
  --cli-binary-format raw-in-base64-out \
  --region "${REGION}" \
  "${RESPONSE_FILE}" >/dev/null

cat "${RESPONSE_FILE}"
echo
