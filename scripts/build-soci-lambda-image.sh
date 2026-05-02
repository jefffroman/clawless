#!/usr/bin/env bash
# Build and push the SOCI index-builder Lambda container image to ECR.
# Mirrors scripts/build-lambda.sh.
#
# Usage: ./scripts/build-soci-lambda-image.sh [--region <region>] [--ecr-repo <url>]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

REGION=""
ECR_REPO_URL=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --region)   REGION="$2";       shift 2 ;;
    --ecr-repo) ECR_REPO_URL="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$REGION" ]]; then
  REGION=$(grep '^aws_region' "$REPO_ROOT/tofu/terraform.tfvars" 2>/dev/null \
    | awk -F'"' '{print $2}' || echo "us-east-1")
fi

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

if [[ -z "$ECR_REPO_URL" ]]; then
  ECR_REPO_URL="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/clawless-soci-index-builder"
fi

IMAGE_URI="${ECR_REPO_URL}:latest"

echo "Building image: $IMAGE_URI"

docker build \
  --platform linux/arm64 \
  --provenance=false \
  -f "$REPO_ROOT/lambda/soci-index-builder/Dockerfile" \
  -t "$IMAGE_URI" \
  "$REPO_ROOT"

docker push "$IMAGE_URI"

if aws lambda get-function --function-name clawless-soci-index-builder \
    --region "$REGION" >/dev/null 2>&1; then
  aws lambda update-function-code \
    --function-name clawless-soci-index-builder \
    --image-uri "$IMAGE_URI" \
    --region "$REGION" >/dev/null
  echo "Lambda function code updated."
else
  echo "Lambda function not yet created — run tofu apply after this script."
fi

echo "Done: $IMAGE_URI"
