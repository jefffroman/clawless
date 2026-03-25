#!/usr/bin/env bash
# bake-snapshot.sh — Provision a golden Lightsail instance, snapshot it, and
# store the snapshot name in tofu/terraform.tfvars as golden_snapshot_name.
#
# The golden snapshot has system packages updated and memory Python packages
# pre-installed. provision-client.yml then runs only the fast config steps.
#
# Usage: ./scripts/bake-snapshot.sh [--region <region>]
#
# Requires: aws CLI, ansible, jq

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TOFU_DIR="$REPO_ROOT/tofu"
ANSIBLE_DIR="$REPO_ROOT/ansible"

hr() { printf '%*s\n' 72 '' | tr ' ' '-'; }
log() { echo "[bake] $*"; }

# ── Parse args ────────────────────────────────────────────────────────────────

REGION=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --region) REGION="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$REGION" ]]; then
  # Read from terraform.tfvars if present
  if [[ -f "$TOFU_DIR/terraform.tfvars" ]]; then
    REGION=$(grep '^aws_region' "$TOFU_DIR/terraform.tfvars" 2>/dev/null \
      | awk -F'"' '{print $2}' || true)
  fi
  REGION="${REGION:-us-east-1}"
fi

INSTANCE_NAME="clawless-golden-bake-$$"
SNAPSHOT_NAME="clawless-golden-$(date +%Y%m%d%H%M%S)"
AZ="${REGION}a"
SSH_KEY="${HOME}/.ssh/clawless_ansible"
KEYPAIR_NAME="clawless-ansible"

hr
log "Baking golden snapshot"
log "  Region:        $REGION"
log "  Instance name: $INSTANCE_NAME"
log "  Snapshot name: $SNAPSHOT_NAME"
hr

# ── Read blueprint and bundle from tfvars ─────────────────────────────────────

BLUEPRINT_ID="ubuntu_24_04"
BUNDLE_ID="medium_3_0"
if [[ -f "$TOFU_DIR/terraform.tfvars" ]]; then
  _bp=$(grep '^lightsail_blueprint_id' "$TOFU_DIR/terraform.tfvars" 2>/dev/null \
    | awk -F'"' '{print $2}' || true)
  _bd=$(grep '^lightsail_bundle_id' "$TOFU_DIR/terraform.tfvars" 2>/dev/null \
    | awk -F'"' '{print $2}' || true)
  BLUEPRINT_ID="${_bp:-$BLUEPRINT_ID}"
  BUNDLE_ID="${_bd:-$BUNDLE_ID}"
fi

# ── Publish ansible to S3 ─────────────────────────────────────────────────────
# Instances pull playbooks from S3 at boot; publish before baking so any new
# clients created from this snapshot pick up the current playbooks immediately.

log "Publishing ansible to S3..."
"$REPO_ROOT/scripts/publish-ansible.sh" --region "$REGION"

# ── Ensure SSH key pair exists (for Ansible SSH access to bake instance) ─────
# Generate locally and upload to Lightsail if not already present.
if [[ ! -f "$SSH_KEY" ]]; then
  log "Generating SSH key pair at $SSH_KEY..."
  ssh-keygen -t ed25519 -f "$SSH_KEY" -C "clawless-ansible" -N ""
fi
PUBLIC_KEY="$(cat "${SSH_KEY}.pub")"
if ! aws lightsail get-key-pair --key-pair-name "$KEYPAIR_NAME" --region "$REGION" >/dev/null 2>&1; then
  log "Uploading key pair '$KEYPAIR_NAME' to Lightsail..."
  aws lightsail import-key-pair \
    --key-pair-name "$KEYPAIR_NAME" \
    --public-key-base64 "$PUBLIC_KEY" \
    --region "$REGION" >/dev/null
fi

# ── Create bake instance ──────────────────────────────────────────────────────

log "Creating bake instance ($BLUEPRINT_ID / $BUNDLE_ID)..."
aws lightsail create-instances \
  --instance-names "$INSTANCE_NAME" \
  --availability-zone "$AZ" \
  --blueprint-id "$BLUEPRINT_ID" \
  --bundle-id "$BUNDLE_ID" \
  --key-pair-name "$KEYPAIR_NAME" \
  --region "$REGION" >/dev/null

cleanup() {
  log "Cleaning up bake resources..."
  aws lightsail delete-instance --instance-name "$INSTANCE_NAME" \
    --force-delete-add-ons --region "$REGION" >/dev/null 2>&1 || true
}
trap cleanup EXIT

log "Waiting for instance to reach running state..."
until aws lightsail get-instance \
    --instance-name "$INSTANCE_NAME" \
    --query 'instance.state.name' \
    --output text --region "$REGION" 2>/dev/null | grep -q running; do
  sleep 5
done

INSTANCE_IP=$(aws lightsail get-instance \
  --instance-name "$INSTANCE_NAME" \
  --query 'instance.publicIpAddress' \
  --output text --region "$REGION")

# ── Open SSH on the bake instance (provisioner IP only) ──────────────────────

PROVISIONER_IP=$(curl -s https://checkip.amazonaws.com)
aws lightsail put-instance-public-ports \
  --instance-name "$INSTANCE_NAME" \
  --port-infos "fromPort=22,toPort=22,protocol=tcp,cidrs=${PROVISIONER_IP}/32" \
  --region "$REGION" >/dev/null

log "Instance running at $INSTANCE_IP — waiting for SSH..."
until ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 \
    -i "$SSH_KEY" "ubuntu@$INSTANCE_IP" true 2>/dev/null; do
  sleep 5
done

log "Waiting for cloud-init to complete (blueprint setup)..."
ssh -o StrictHostKeyChecking=no -i "$SSH_KEY" "ubuntu@$INSTANCE_IP" \
  "sudo cloud-init status --wait" || true

# ── Run base provisioning playbook ───────────────────────────────────────────

log "Running provision-base.yml..."
cd "$ANSIBLE_DIR"
ansible-playbook \
  -i "$INSTANCE_IP," \
  -e "golden_snapshot_name=$SNAPSHOT_NAME" \
  playbooks/provision-base.yml

# ── Clear SSM registration before snapshot ────────────────────────────────────
# New clients created from this snapshot must register with their own per-client
# activation. The user_data script in null_resource.instance_from_snapshot handles
# this on first boot, but only if the registration file is absent.

log "Clearing SSM registration from bake instance..."
ssh -o StrictHostKeyChecking=no -i "$SSH_KEY" "ubuntu@$INSTANCE_IP" \
  "sudo systemctl stop snap.amazon-ssm-agent.amazon-ssm-agent && sudo rm -f /var/lib/amazon/ssm/registration"

# ── Stop instance and take snapshot ──────────────────────────────────────────

log "Stopping instance for clean snapshot..."
aws lightsail stop-instance --instance-name "$INSTANCE_NAME" --region "$REGION" >/dev/null
until aws lightsail get-instance \
    --instance-name "$INSTANCE_NAME" \
    --query 'instance.state.name' \
    --output text --region "$REGION" 2>/dev/null | grep -q stopped; do
  sleep 5
done

log "Taking snapshot ($SNAPSHOT_NAME)..."
aws lightsail create-instance-snapshot \
  --instance-name "$INSTANCE_NAME" \
  --instance-snapshot-name "$SNAPSHOT_NAME" \
  --region "$REGION" >/dev/null

log "Waiting for snapshot to be available..."
until aws lightsail get-instance-snapshot \
    --instance-snapshot-name "$SNAPSHOT_NAME" \
    --query 'instanceSnapshot.state' \
    --output text --region "$REGION" 2>/dev/null | grep -q available; do
  sleep 10
done

# ── Write snapshot name to terraform.tfvars ───────────────────────────────────

TFVARS="$TOFU_DIR/terraform.tfvars"
if grep -q '^golden_snapshot_name' "$TFVARS" 2>/dev/null; then
  sed -i.bak "s|^golden_snapshot_name.*|golden_snapshot_name = \"$SNAPSHOT_NAME\"|" "$TFVARS"
  rm -f "${TFVARS}.bak"
else
  echo "golden_snapshot_name = \"$SNAPSHOT_NAME\"" >> "$TFVARS"
fi

# Upload updated tfvars to S3 so the lifecycle Lambda picks up the new snapshot.
STATE_BUCKET="clawless-tfstate-${ACCOUNT_ID}"
aws s3 cp "$TFVARS" "s3://${STATE_BUCKET}/config/terraform.tfvars"
log "terraform.tfvars uploaded to s3://${STATE_BUCKET}/config/terraform.tfvars"

log "Running tofu apply to register new snapshot name in state..."
cd "$TOFU_DIR"
tofu apply -auto-approve -input=false

hr
log "Golden snapshot ready: $SNAPSHOT_NAME"
log "terraform.tfvars updated and tofu state reflects new golden_snapshot_name."
hr

# cleanup runs via trap EXIT
