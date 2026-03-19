#!/usr/bin/env bash
# publish-ansible.sh — Sync the ansible directory to S3 so instances can pull
# playbooks at boot without requiring a golden snapshot rebake.
#
# Usage: ./scripts/publish-ansible.sh [--region <region>]
#
# ansible_s3_bucket must be set in tofu/terraform.tfvars.
# Run this whenever playbooks or roles change. No rebake needed.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TOFU_DIR="$REPO_ROOT/tofu"
ANSIBLE_DIR="$REPO_ROOT/ansible"

log() { echo "[publish-ansible] $*"; }

# ── Parse args ────────────────────────────────────────────────────────────────

REGION=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --region) REGION="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$REGION" ]]; then
  REGION=$(grep '^aws_region' "$TOFU_DIR/terraform.tfvars" 2>/dev/null \
    | awk -F'"' '{print $2}' || true)
  REGION="${REGION:-us-east-1}"
fi

BUCKET=$(grep '^ansible_s3_bucket' "$TOFU_DIR/terraform.tfvars" 2>/dev/null \
  | awk -F'"' '{print $2}' || true)

if [[ -z "$BUCKET" ]]; then
  echo "ERROR: ansible_s3_bucket not set in tofu/terraform.tfvars" >&2
  exit 1
fi

# ── Sync ──────────────────────────────────────────────────────────────────────

log "Syncing ansible/ → s3://$BUCKET/ansible/ (region: $REGION)..."

aws s3 sync "$ANSIBLE_DIR/" "s3://$BUCKET/ansible/" \
  --region "$REGION" \
  --exclude "*.pyc" \
  --exclude "__pycache__/*" \
  --exclude "*.retry" \
  --exclude "inventory/hosts.yml" \
  --delete

log "Done. Instances will pull from s3://$BUCKET/ansible/ at boot."
