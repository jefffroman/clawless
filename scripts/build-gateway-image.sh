#!/usr/bin/env bash
# Build and push the clawless Fargate gateway container image to ECR.
# Mirrors scripts/build-lambda.sh.
#
# Usage: ./scripts/build-gateway-image.sh [--region <region>] [--ecr-repo <url>] [--tag <tag>]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

REGION=""
ECR_REPO_URL=""
TAG="latest"
NO_PUSH=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --region)   REGION="$2";       shift 2 ;;
    --ecr-repo) ECR_REPO_URL="$2"; shift 2 ;;
    --tag)      TAG="$2";          shift 2 ;;
    --no-push)  NO_PUSH=true;      shift 1 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$REGION" ]]; then
  REGION=$(grep '^aws_region' "$REPO_ROOT/tofu/terraform.tfvars" 2>/dev/null \
    | awk -F'"' '{print $2}' || echo "us-east-1")
fi

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

if [[ -z "$ECR_REPO_URL" ]]; then
  ECR_REPO_URL="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/clawless-gateway"
fi

IMAGE_URI="${ECR_REPO_URL}:${TAG}"

echo "Building image: $IMAGE_URI"

# Auth is handled by docker-credential-ecr-login via credHelpers in
# ~/.docker/config.json — no explicit `docker login` needed. The helper
# fetches fresh creds per push/pull using the ambient AWS profile.

docker build \
  --platform linux/arm64 \
  --provenance=false \
  -f "$REPO_ROOT/docker/gateway/Dockerfile" \
  -t "$IMAGE_URI" \
  "$REPO_ROOT"

if [[ "$NO_PUSH" == "true" ]]; then
  echo "Built (not pushed): $IMAGE_URI"
else
  docker push "$IMAGE_URI"
  echo "Done: $IMAGE_URI"
fi
