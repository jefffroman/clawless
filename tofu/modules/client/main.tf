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
  tags = merge(var.tags, {
    Client = var.client_slug
    Active = tostring(var.active)
  })
}

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

# ── S3 Workspace Backup (primary region) ──────────────────────────────────────

resource "aws_s3_bucket" "workspace_backup" {
  bucket = "${local.name_prefix}-backup-${data.aws_caller_identity.current.account_id}"
  tags   = local.tags

  lifecycle {
    prevent_destroy = true
  }
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

  lifecycle {
    prevent_destroy = true
  }
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

# ── Lightsail Instance ────────────────────────────────────────────────────────

resource "aws_lightsail_instance" "this" {
  count = var.active ? 1 : 0

  name              = local.name_prefix
  availability_zone = var.availability_zone
  blueprint_id      = var.blueprint_id
  bundle_id         = var.bundle_id
  key_pair_name     = var.key_pair_name

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

# ── Lightsail Instance Ready Wait ─────────────────────────────────────────────
# Lightsail rejects PutInstancePublicPorts while the instance is in "pending"
# state. Poll until "running" before proceeding with firewall configuration.

resource "null_resource" "instance_running" {
  count = var.active ? 1 : 0

  triggers = {
    instance_id = aws_lightsail_instance.this[0].id
  }

  depends_on = [aws_lightsail_instance.this]

  provisioner "local-exec" {
    command = <<-EOT
      until aws lightsail get-instance \
        --instance-name ${aws_lightsail_instance.this[0].name} \
        --query 'instance.state.name' \
        --output text 2>/dev/null | grep -q running; do
        sleep 5
      done
    EOT
  }
}

# ── Lightsail Firewall ────────────────────────────────────────────────────────
# All setup ports restricted to the provisioner's IP.
# NOTE: if webhook-based channel integrations are used, port 443 will need
# to be opened to 0.0.0.0/0 — update provisioner_cidr to ["0.0.0.0/0"]
# for the 443 rule at that point.

resource "aws_lightsail_instance_public_ports" "this" {
  count = var.active ? 1 : 0

  depends_on    = [null_resource.instance_running]
  instance_name = aws_lightsail_instance.this[0].name

  lifecycle {
    replace_triggered_by = [null_resource.instance_running]
  }

  port_info {
    protocol  = "tcp"
    from_port = 22
    to_port   = 22
    cidrs     = [var.provisioner_cidr]
  }

  port_info {
    protocol  = "tcp"
    from_port = 80
    to_port   = 80
    cidrs     = [var.provisioner_cidr]
  }

  port_info {
    protocol  = "tcp"
    from_port = 443
    to_port   = 443
    cidrs     = [var.provisioner_cidr]
  }
}
