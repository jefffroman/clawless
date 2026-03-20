terraform {
  required_providers {
    aws = {
      source                = "hashicorp/aws"
      configuration_aliases = [aws.backup]
    }
  }
}

locals {
  name_prefix = "clawless-${var.client_slug}"

  # Discover per-client pause snapshot by fixed name convention.
  # Falls back to golden snapshot, then blueprint (empty string = blueprint path).
  client_snap   = data.external.client_snapshot.result.state == "available" ? data.external.client_snapshot.result.name : ""
  snapshot_name = coalesce(local.client_snap, var.golden_snapshot_name, "")
  use_snapshot  = local.snapshot_name != ""

  tags = merge(var.tags, {
    Client = var.client_slug
    Active = tostring(var.active)
  })
}

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

# Discover a per-client pause snapshot by fixed name (clawless-{slug}-snap).
# Returns state="available" and name if found; empty strings otherwise.
# pause.sh creates this snapshot; resume.sh deletes it after restore.
data "external" "client_snapshot" {
  program = [
    "bash", "-c",
    "aws lightsail get-instance-snapshot --instance-snapshot-name clawless-${var.client_slug}-snap --region ${data.aws_region.current.name} --query '{\"name\":instanceSnapshot.name,\"state\":instanceSnapshot.state}' --output json 2>/dev/null || printf '{\"name\":\"\",\"state\":\"\"}'"
  ]
}

# ── S3 Workspace Backup (primary region) ──────────────────────────────────────

resource "aws_s3_bucket" "workspace_backup" {
  bucket = "${local.name_prefix}-backup-${data.aws_caller_identity.current.account_id}"
  tags   = local.tags

}

resource "aws_s3_bucket_versioning" "workspace_backup" {
  bucket = aws_s3_bucket.workspace_backup.id
  versioning_configuration {
    status = "Enabled" # Required for CRR source
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "workspace_backup" {
  bucket = aws_s3_bucket.workspace_backup.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "workspace_backup" {
  bucket                  = aws_s3_bucket.workspace_backup.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "workspace_backup" {
  bucket = aws_s3_bucket.workspace_backup.id

  rule {
    id     = "expire-old-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 7
    }

    expiration {
      expired_object_delete_marker = true
    }
  }

  depends_on = [aws_s3_bucket_versioning.workspace_backup]
}

# ── S3 Replica Bucket (backup region) ────────────────────────────────────────

resource "aws_s3_bucket" "workspace_backup_replica" {
  provider = aws.backup
  bucket   = "${local.name_prefix}-backup-replica-${data.aws_caller_identity.current.account_id}"
  tags     = local.tags

}

resource "aws_s3_bucket_versioning" "workspace_backup_replica" {
  provider = aws.backup
  bucket   = aws_s3_bucket.workspace_backup_replica.id
  versioning_configuration {
    status = "Enabled" # Required for CRR destination
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "workspace_backup_replica" {
  provider = aws.backup
  bucket   = aws_s3_bucket.workspace_backup_replica.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "workspace_backup_replica" {
  provider                = aws.backup
  bucket                  = aws_s3_bucket.workspace_backup_replica.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "workspace_backup_replica" {
  provider = aws.backup
  bucket   = aws_s3_bucket.workspace_backup_replica.id

  rule {
    id     = "expire-old-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 7
    }

    expiration {
      expired_object_delete_marker = true
    }
  }

  depends_on = [aws_s3_bucket_versioning.workspace_backup_replica]
}

# ── CRR Replication Role ──────────────────────────────────────────────────────

data "aws_iam_policy_document" "replication_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["s3.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "replication" {
  statement {
    effect    = "Allow"
    actions   = ["s3:GetReplicationConfiguration", "s3:ListBucket"]
    resources = [aws_s3_bucket.workspace_backup.arn]
  }

  statement {
    effect    = "Allow"
    actions   = ["s3:GetObjectVersionForReplication", "s3:GetObjectVersionAcl", "s3:GetObjectVersionTagging"]
    resources = ["${aws_s3_bucket.workspace_backup.arn}/*"]
  }

  statement {
    effect    = "Allow"
    actions   = ["s3:ReplicateObject", "s3:ReplicateDelete", "s3:ReplicateTags"]
    resources = ["${aws_s3_bucket.workspace_backup_replica.arn}/*"]
  }
}

resource "aws_iam_role" "replication" {
  name               = "${local.name_prefix}-s3-replication"
  assume_role_policy = data.aws_iam_policy_document.replication_assume.json
  tags               = local.tags
}

resource "aws_iam_role_policy" "replication" {
  name   = "s3-replication"
  role   = aws_iam_role.replication.id
  policy = data.aws_iam_policy_document.replication.json
}

resource "aws_s3_bucket_replication_configuration" "workspace_backup" {
  bucket = aws_s3_bucket.workspace_backup.id
  role   = aws_iam_role.replication.arn

  rule {
    id     = "replicate-workspace"
    status = "Enabled"

    destination {
      bucket        = aws_s3_bucket.workspace_backup_replica.arn
      storage_class = "STANDARD_IA" # Cheaper for replica we hope never to need
    }
  }

  depends_on = [aws_s3_bucket_versioning.workspace_backup]
}

# ── IAM Role (SSM trust) ──────────────────────────────────────────────────────
# Lightsail has no native instance profile support, so we use SSM Hybrid
# Activation. The instance registers with SSM on first boot via user_data;
# SSM then issues rotating 1-hour temporary credentials automatically.
# No long-lived access keys are created or stored anywhere.

data "aws_iam_policy_document" "ssm_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ssm.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "bedrock" {
  statement {
    sid       = "BedrockInvokeModel"
    effect    = "Allow"
    actions   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = ["*"]
  }
}

data "aws_iam_policy_document" "s3_backup" {
  statement {
    sid    = "WorkspaceBackup"
    effect = "Allow"
    actions = [
      "s3:PutObject",
      "s3:GetObject",
      "s3:DeleteObject",
      "s3:ListBucket",
    ]
    resources = [
      aws_s3_bucket.workspace_backup.arn,
      "${aws_s3_bucket.workspace_backup.arn}/*",
    ]
  }
}

resource "aws_iam_role" "ssm" {
  name               = "${local.name_prefix}-ssm"
  assume_role_policy = data.aws_iam_policy_document.ssm_assume.json
  tags               = local.tags
}

resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.ssm.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "bedrock" {
  name   = "bedrock-invoke"
  role   = aws_iam_role.ssm.id
  policy = data.aws_iam_policy_document.bedrock.json
}

resource "aws_iam_role_policy" "s3_backup" {
  name   = "s3-workspace-backup"
  role   = aws_iam_role.ssm.id
  policy = data.aws_iam_policy_document.s3_backup.json
}

data "aws_iam_policy_document" "s3_ansible" {
  statement {
    sid    = "AnsibleRead"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:ListBucket",
    ]
    resources = [
      "arn:aws:s3:::${var.ansible_s3_bucket}",
      "arn:aws:s3:::${var.ansible_s3_bucket}/ansible/*",
    ]
  }
}

resource "aws_iam_role_policy" "s3_ansible" {
  name   = "s3-ansible-read"
  role   = aws_iam_role.ssm.id
  policy = data.aws_iam_policy_document.s3_ansible.json
}

data "aws_iam_policy_document" "cloudwatch_backup" {
  statement {
    sid       = "BackupMetrics"
    effect    = "Allow"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["Clawless/Backup"]
    }
  }
}

resource "aws_iam_role_policy" "cloudwatch_backup" {
  name   = "cloudwatch-backup-metrics"
  role   = aws_iam_role.ssm.id
  policy = data.aws_iam_policy_document.cloudwatch_backup.json
}

# ── SSM Activation ────────────────────────────────────────────────────────────

resource "aws_ssm_activation" "this" {
  count = var.active ? 1 : 0

  name               = local.name_prefix
  iam_role           = aws_iam_role.ssm.name
  registration_limit = 5 # Buffer for instance recreation cycles; each new instance uses one slot

  depends_on = [aws_iam_role_policy_attachment.ssm_core]

  lifecycle {
    ignore_changes = [expiration_date]
  }
}

# ── Lightsail Instance (blueprint path) ───────────────────────────────────────
# Used when no snapshot is configured — very first setup before a golden bake exists.

resource "aws_lightsail_instance" "this" {
  count = var.active && !local.use_snapshot ? 1 : 0

  name              = local.name_prefix
  availability_zone = var.availability_zone
  blueprint_id      = var.blueprint_id
  bundle_id         = var.bundle_id

  user_data = <<-EOT
    # Clawless: SSM Hybrid Activation registration
    # Note: this script is appended to the blueprint's /bin/sh init script,
    # so we use POSIX sh syntax throughout.
    set -eu
    snap install amazon-ssm-agent --classic
    /snap/amazon-ssm-agent/current/amazon-ssm-agent -register -y \
      -id "${aws_ssm_activation.this[0].id}" \
      -code "${aws_ssm_activation.this[0].activation_code}" \
      -region "${data.aws_region.current.name}"
    systemctl enable snap.amazon-ssm-agent.amazon-ssm-agent
    systemctl restart snap.amazon-ssm-agent.amazon-ssm-agent
  EOT

  tags = local.tags
}

# ── Lightsail Instance (snapshot path) ────────────────────────────────────────
# Used for both new clients (golden snapshot) and resume (per-client snapshot).
# The AWS provider does not support snapshot-based creation in aws_lightsail_instance,
# so we drive it via the CLI.
#
# user_data is identical for both cases: the script re-registers the SSM agent
# only if /var/lib/amazon/ssm/registration is absent (golden path, cleared during
# bake). Resume instances carry their mi-XXXX identity in the snapshot, so the
# check is a no-op and they reconnect automatically.

resource "null_resource" "instance_from_snapshot" {
  count = var.active && local.use_snapshot ? 1 : 0

  triggers = {
    instance_name = local.name_prefix
    snapshot_name = local.snapshot_name
    region        = data.aws_region.current.name
  }

  depends_on = [aws_ssm_activation.this]

  provisioner "local-exec" {
    command = <<-EOT
      set -e
      _tmpud=$(mktemp /tmp/clawless-userdata-XXXXXX.sh)
      trap 'rm -f "$_tmpud"' EXIT
      cat > "$_tmpud" <<'USERDATA'
set -eu

# SSM registration: skipped for resume (registration file present in per-client snapshot)
if [ ! -s /var/lib/amazon/ssm/registration ]; then
  /snap/amazon-ssm-agent/current/amazon-ssm-agent -register -y \
    -id ${try(aws_ssm_activation.this[0].id, "")} \
    -code ${try(aws_ssm_activation.this[0].activation_code, "")} \
    -region ${data.aws_region.current.name}
  systemctl restart snap.amazon-ssm-agent.amazon-ssm-agent
fi

# Ansible provisioning: skipped for resume (sentinel file present in per-client snapshot)
# Client vars are embedded at tofu apply time — no AWS API calls needed from the instance.
if [ ! -f /var/lib/openclaw/.provisioned ]; then
  base64 -d > /tmp/clawless-client-vars.json <<'CLIENTVARS'
${base64encode(jsonencode({
  client_slug             = var.client_slug
  display_name            = var.display_name
  openclaw_bedrock_region = data.aws_region.current.name
  openclaw_backup_bucket  = aws_s3_bucket.workspace_backup.id
  agent_name              = var.agent_name
  agent_style             = var.agent_style
  agent_channel           = var.agent_channel
  channel_config          = var.channel_config
}))}
CLIENTVARS
  cd /opt/clawless/ansible
  ansible-playbook playbooks/provision-client.yml \
    -i localhost, \
    -c local \
    -e "@/tmp/clawless-client-vars.json"
fi
USERDATA
      aws lightsail create-instances-from-snapshot \
        --instance-names "${local.name_prefix}" \
        --availability-zone "${var.availability_zone}" \
        --bundle-id "${var.bundle_id}" \
        --instance-snapshot-name "${local.snapshot_name}" \
        --user-data "file://$_tmpud"
    EOT
  }

  provisioner "local-exec" {
    when    = destroy
    # delete-instance is async; poll until the instance is gone so a same-name recreate doesn't collide.
    command = <<-EOT
      aws lightsail delete-instance \
        --instance-name ${self.triggers.instance_name} \
        --force-delete-add-ons \
        --region ${self.triggers.region} 2>/dev/null || true
      for i in $(seq 1 60); do
        aws lightsail get-instance \
          --instance-name ${self.triggers.instance_name} \
          --region ${self.triggers.region} \
          --output text 2>/dev/null || break
        sleep 5
      done
      sleep 10
    EOT
  }
}

# ── Lightsail Instance Ready Wait ─────────────────────────────────────────────
# Lightsail rejects PutInstancePublicPorts while the instance is in "pending"
# state. Poll until "running" before proceeding with firewall configuration.
# Instance name is deterministic (clawless-{slug}) regardless of creation path.

resource "null_resource" "instance_running" {
  count = var.active ? 1 : 0

  triggers = {
    # Use the creation resource's ID (not snapshot name) so this re-runs whenever the
    # instance is replaced, even when recreated from the same snapshot.
    instance_ref = local.use_snapshot ? try(null_resource.instance_from_snapshot[0].id, "") : try(aws_lightsail_instance.this[0].id, "")
  }

  depends_on = [aws_lightsail_instance.this, null_resource.instance_from_snapshot]

  provisioner "local-exec" {
    command = <<-EOT
      until aws lightsail get-instance \
        --instance-name ${local.name_prefix} \
        --query 'instance.state.name' \
        --output text 2>/dev/null | grep -q running; do
        sleep 5
      done
    EOT
  }
}

# ── Lightsail Firewall ────────────────────────────────────────────────────────
# All inbound ports are closed. No services listen on public interfaces:
# - Admin access is via SSM Session Manager (no port 22)
# - OpenClaw gateway binds to loopback only (no port 443)
# - Channel integrations (Telegram, Discord, Slack) use outbound connections
#
# If a webhook-based integration is ever added, open port 443 to 0.0.0.0/0
# here and add a TLS-terminating reverse proxy + webhook auth on the instance.
