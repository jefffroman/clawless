#!/usr/bin/env bash
# Creates the S3 bucket used for Terraform/OpenTofu remote state.
# Run once before the first `tofu init`. Safe to re-run — bucket creation
# is idempotent and existing state is never touched.
set -euo pipefail

REGION="${1:-us-east-1}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
BUCKET="clawless-tfstate-${ACCOUNT_ID}"

echo "Account : ${ACCOUNT_ID}"
echo "Region  : ${REGION}"
echo "Bucket  : ${BUCKET}"
echo

# Create bucket (us-east-1 must omit LocationConstraint)
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
    "Rules": [{
      "ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "aws:kms"},
      "BucketKeyEnabled": true
    }]
  }'

aws s3api put-public-access-block \
  --bucket "${BUCKET}" \
  --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"

# Write backend.hcl for tofu init -backend-config
BACKEND_HCL="$(dirname "$0")/../terraform/backend.hcl"
cat > "${BACKEND_HCL}" <<EOF
bucket  = "${BUCKET}"
region  = "${REGION}"
EOF

echo "State bucket ready."
echo "backend.hcl written to terraform/backend.hcl"
echo
echo "Commit backend.hcl, then: cd terraform && tofu init -backend-config=backend.hcl"
