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
BACKEND_HCL="$(dirname "$0")/../tofu/backend.hcl"
cat > "${BACKEND_HCL}" <<EOF
bucket  = "${BUCKET}"
region  = "${REGION}"
EOF

# Create the clients SSM parameter if it doesn't exist.
# The storefront Lambda writes to this on signup; for testing, populate manually:
#   aws ssm put-parameter --name /clawless/clients --type String --overwrite \
#     --value '{"test":{"display_name":"Test Client","active":true}}'
aws ssm put-parameter \
  --name "/clawless/clients" \
  --type "String" \
  --value "{}" \
  --region "${REGION}" 2>/dev/null \
  && echo "Created SSM parameter /clawless/clients (empty)" \
  || echo "SSM parameter /clawless/clients already exists — skipping"

echo
echo "State bucket ready."
echo "backend.hcl written to tofu/backend.hcl"
echo
echo "Populate /clawless/clients before running tofu plan:"
echo "  aws ssm put-parameter --name /clawless/clients --type String --overwrite \\"
echo "    --value '{\"test\":{\"display_name\":\"Test Client\",\"active\":true}}'"
echo
echo "Then: cd tofu && tofu init -backend-config=backend.hcl"
