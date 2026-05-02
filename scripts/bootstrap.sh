#!/usr/bin/env bash
# One-time setup: S3 state bucket, backend.hcl, terraform.tfvars, first agent.
# Safe to re-run — AWS resource creation is idempotent; tfvars is overwritten.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TOFU_DIR="${SCRIPT_DIR}/../tofu"

hr()  { echo "────────────────────────────────────────────────────────"; }
ask() { # ask <var> <prompt> [default]
  local __var="$1" __prompt="$2" __default="${3:-}" __val=""
  if [[ -n "$__default" ]]; then
    read -rp "${__prompt} [${__default}]: " __val
    printf -v "$__var" '%s' "${__val:-$__default}"
  else
    while [[ -z "$__val" ]]; do
      read -rp "${__prompt}: " __val
    done
    printf -v "$__var" '%s' "$__val"
  fi
}

# ── AWS identity ──────────────────────────────────────────────────────────────
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
ask REGION "AWS region" "us-east-1"
BUCKET="clawless-tfstate-${ACCOUNT_ID}"

hr
echo "Account : ${ACCOUNT_ID}"
echo "Region  : ${REGION}"
echo "Bucket  : ${BUCKET}"
hr

# ── S3 state bucket ───────────────────────────────────────────────────────────
# us-east-1 buckets must omit LocationConstraint — all other regions require it.
if [[ "${REGION}" == "us-east-1" ]]; then
  aws s3api create-bucket \
    --bucket "${BUCKET}" \
    --region "${REGION}" 2>/dev/null || true
else
  aws s3api create-bucket \
    --bucket "${BUCKET}" \
    --region "${REGION}" \
    --create-bucket-configuration LocationConstraint="${REGION}" 2>/dev/null || true
fi

aws s3api put-bucket-versioning \
  --bucket "${BUCKET}" \
  --versioning-configuration Status=Enabled

aws s3api put-bucket-encryption \
  --bucket "${BUCKET}" \
  --server-side-encryption-configuration '{
    "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "aws:kms"},
               "BucketKeyEnabled": true}]}'

aws s3api put-public-access-block \
  --bucket "${BUCKET}" \
  --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"

aws s3api put-bucket-lifecycle-configuration \
  --bucket "${BUCKET}" \
  --lifecycle-configuration '{
    "Rules": [{
      "ID": "expire-old-versions",
      "Status": "Enabled",
      "Filter": {},
      "NoncurrentVersionExpiration": {"NoncurrentDays": 7},
      "Expiration": {"ExpiredObjectDeleteMarker": true}
    }]}'

echo "State bucket ready: ${BUCKET}"

# ── backend.hcl ───────────────────────────────────────────────────────────────
cat > "${TOFU_DIR}/backend.hcl" <<EOF
bucket  = "${BUCKET}"
region  = "${REGION}"
EOF
echo "backend.hcl written"

# ── Alert email ───────────────────────────────────────────────────────────────
hr
ask ALERT_EMAIL "Alert email (Bedrock budget and backup failure notifications)"

cat > "${TOFU_DIR}/terraform.tfvars" <<EOF
alert_email = "${ALERT_EMAIL}"
EOF
echo "terraform.tfvars written"

# ── Upload tfvars to S3 for Lambda access ─────────────────────────────────────
aws s3 cp "${TOFU_DIR}/terraform.tfvars" "s3://${BUCKET}/config/terraform.tfvars"
echo "terraform.tfvars uploaded to s3://${BUCKET}/config/terraform.tfvars"

# ── SSM config parameters ─────────────────────────────────────────────────────
# Agent records live under /clawless/clients/{client_slug}/{agent_slug} and are
# created by add-agent.sh (called below). No bootstrap needed for that hierarchy.

CLAWLESS_VERSION="$(git describe --tags --exact-match 2>/dev/null || git rev-parse --short HEAD)"
aws ssm put-parameter \
  --name "/clawless/version" \
  --type "String" \
  --value "${CLAWLESS_VERSION}" \
  --overwrite \
  --region "${REGION}"
echo "Version set to ${CLAWLESS_VERSION} (from current git ref)"

# ── Next steps ────────────────────────────────────────────────────────────────
hr
echo "Bootstrap complete. Next steps:"
echo
echo "  ./scripts/build-gateway-image.sh  # build and push Fargate gateway image"
echo "  ./scripts/build-searxng-image.sh  # build and push SearXNG Lambda image"
echo "  ./scripts/build-lambda.sh         # build and push lifecycle Lambda image"
echo "  cd tofu && tofu init -backend-config=backend.hcl && tofu apply"
echo "  ./scripts/add-agent.sh            # add your first agent"
hr
