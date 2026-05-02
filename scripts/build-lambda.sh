#!/usr/bin/env bash
# Build and push the clawless lifecycle Lambda container image to ECR.
# Run this before the first `tofu apply` and after any changes to
# lambda/Dockerfile or lambda/handler.py.
#
# Usage: ./scripts/build-lambda.sh [--region <region>] [--ecr-repo <url>]
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
  ECR_REPO_URL="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/clawless-lifecycle"
fi

IMAGE_URI="${ECR_REPO_URL}:latest"

echo "Building image: $IMAGE_URI"

# Auth is handled by docker-credential-ecr-login via credHelpers in
# ~/.docker/config.json — no explicit `docker login` needed. The helper
# fetches fresh creds per push/pull using the ambient AWS profile.

# Build (context is repo root so Dockerfile can COPY from tofu/ and lambda/)
docker build \
  --platform linux/arm64 \
  --provenance=false \
  -f "$REPO_ROOT/lambda/Dockerfile" \
  -t "$IMAGE_URI" \
  "$REPO_ROOT"

docker push "$IMAGE_URI"

# Update running Lambda if it already exists
if aws lambda get-function --function-name clawless-lifecycle \
    --region "$REGION" >/dev/null 2>&1; then
  aws lambda update-function-code \
    --function-name clawless-lifecycle \
    --image-uri "$IMAGE_URI" \
    --region "$REGION" >/dev/null
  echo "Lambda function code updated."
else
  echo "Lambda function not yet created — run tofu apply after this script."
fi

echo "Done: $IMAGE_URI"
