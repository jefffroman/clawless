#!/usr/bin/env bash
# Build and push the clawless Fargate gateway container image to ECR.
# Mirrors scripts/build-lambda.sh.
#
# Flow:
#   1. docker build
#   2. docker push with :candidate-<git-sha> tag (never :latest directly)
#   3. Synchronously invoke the SOCI index-builder Lambda with the image
#      digest. The Lambda builds the lazy-load index and — only on success —
#      re-tags the digest as :latest. On failure, :latest is untouched and
#      the candidate tag lingers so the break is visible in ECR.
#
# Usage: ./scripts/build-gateway-image.sh [--region <region>] [--ecr-repo <url>] [--no-push] [--no-soci]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

REGION=""
ECR_REPO_URL=""
NO_PUSH=false
NO_SOCI=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --region)   REGION="$2";       shift 2 ;;
    --ecr-repo) ECR_REPO_URL="$2"; shift 2 ;;
    --no-push)  NO_PUSH=true;      shift 1 ;;
    --no-soci)  NO_SOCI=true;      shift 1 ;;
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

REPO_NAME="${ECR_REPO_URL##*/}"

# Candidate tag: short git SHA, plus -dirty if the tree has uncommitted
# changes. Never :latest — the SOCI Lambda owns that tag.
GIT_SHA=$(git -C "$REPO_ROOT" rev-parse --short HEAD)
if ! git -C "$REPO_ROOT" diff-index --quiet HEAD --; then
  GIT_SHA="${GIT_SHA}-dirty"
fi
CANDIDATE_TAG="candidate-${GIT_SHA}"
IMAGE_URI="${ECR_REPO_URL}:${CANDIDATE_TAG}"

echo "Building image: $IMAGE_URI"

# Auth is handled by docker-credential-ecr-login via credHelpers in
# ~/.docker/config.json — no explicit `docker login` needed.

docker build \
  --platform linux/arm64 \
  --provenance=false \
  -f "$REPO_ROOT/docker/gateway/Dockerfile" \
  -t "$IMAGE_URI" \
  "$REPO_ROOT"

if [[ "$NO_PUSH" == "true" ]]; then
  echo "Built (not pushed): $IMAGE_URI"
  exit 0
fi

docker push "$IMAGE_URI"

# Resolve the manifest digest from the candidate tag. We can't rely on
# `docker push` output parsing because BuildKit's format varies; ECR is the
# source of truth.
DIGEST=$(aws ecr describe-images \
  --region "$REGION" \
  --repository-name "$REPO_NAME" \
  --image-ids imageTag="$CANDIDATE_TAG" \
  --query 'imageDetails[0].imageDigest' \
  --output text)

echo "Pushed: $IMAGE_URI (digest: $DIGEST)"

if [[ "$NO_SOCI" == "true" ]]; then
  echo "Skipping SOCI index (--no-soci). :latest NOT promoted."
  exit 0
fi

echo "Invoking SOCI index-builder Lambda (synchronous; ~60-120s)..."
PAYLOAD=$(mktemp)
RESPONSE=$(mktemp)
trap 'rm -f "$PAYLOAD" "$RESPONSE"' EXIT

cat > "$PAYLOAD" <<EOF
{
  "repository":    "$REPO_NAME",
  "digest":        "$DIGEST",
  "candidate_tag": "$CANDIDATE_TAG",
  "promote_tag":   "latest",
  "region":        "$REGION"
}
EOF

META=$(aws lambda invoke \
  --function-name clawless-soci-index-builder \
  --region "$REGION" \
  --payload "fileb://$PAYLOAD" \
  --cli-read-timeout 900 \
  "$RESPONSE")

STATUS=$(echo "$META" | grep -o '"StatusCode":[[:space:]]*[0-9]*' | awk -F: '{print $2}' | tr -d ' ')
ERROR=$(echo "$META" | grep -o '"FunctionError":[[:space:]]*"[^"]*"' | sed 's/.*"FunctionError":[[:space:]]*"\([^"]*\)"/\1/')

if [[ "$STATUS" != "200" || -n "$ERROR" ]]; then
  echo "SOCI build FAILED. :latest unchanged."
  echo "Lambda metadata: $META"
  echo "Lambda response: $(cat "$RESPONSE")"
  exit 1
fi

echo "SOCI build OK:"
cat "$RESPONSE"
echo

# The converted image has a NEW digest (SOCI v2 produces a new image, not
# a sidecar artifact). Report that instead of the pre-SOCI candidate digest.
CONVERTED_DIGEST=$(grep -o '"converted_digest":[[:space:]]*"[^"]*"' "$RESPONSE" \
  | sed 's/.*"converted_digest":[[:space:]]*"\([^"]*\)"/\1/')
echo "Done: ${ECR_REPO_URL}:latest @ ${CONVERTED_DIGEST:-$DIGEST}"
