#!/bin/bash
# Refresh OpenClaw AWS credentials from the managed instance role.
# Runs via SSM State Manager in the managed instance credential context
# (root, with clawless-{slug}-ssm IAM role credentials).
#
# Calls sts:AssumeRole on the same role (self-assume) to obtain a fresh
# 1-hour session, then writes it to /home/ubuntu/.aws/credentials as the
# [default] profile so all ubuntu processes (openclaw-gateway, backup cron)
# use the correct per-client IAM role without touching IMDS.
set -euo pipefail

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
ROLE_NAME=$(aws sts get-caller-identity --query Arn --output text \
  | sed 's|.*assumed-role/||;s|/.*||')
ROLE_ARN="arn:aws:iam::$ACCOUNT:role/$ROLE_NAME"

CREDS=$(aws sts assume-role \
  --role-arn "$ROLE_ARN" \
  --role-session-name "openclaw-$(date +%s)" \
  --duration-seconds 3600 \
  --output json \
  --query Credentials)

AKI=$(printf '%s' "$CREDS" | python3 -c "import sys,json; c=json.load(sys.stdin); print(c['AccessKeyId'])")
SAK=$(printf '%s' "$CREDS" | python3 -c "import sys,json; c=json.load(sys.stdin); print(c['SecretAccessKey'])")
ST=$(printf '%s'  "$CREDS" | python3 -c "import sys,json; c=json.load(sys.stdin); print(c['SessionToken'])")
EXP=$(printf '%s' "$CREDS" | python3 -c "import sys,json; c=json.load(sys.stdin); print(c['Expiration'])")

mkdir -p /home/ubuntu/.aws
printf '[default]\naws_access_key_id = %s\naws_secret_access_key = %s\naws_session_token = %s\n' \
  "$AKI" "$SAK" "$ST" > /home/ubuntu/.aws/credentials
chmod 600 /home/ubuntu/.aws/credentials
chown ubuntu:ubuntu /home/ubuntu/.aws /home/ubuntu/.aws/credentials

echo "Credentials refreshed; expire at $EXP"
