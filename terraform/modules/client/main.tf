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
  })
}

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

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

# ── SSM Activation ────────────────────────────────────────────────────────────

resource "aws_ssm_activation" "this" {
  name               = local.name_prefix
  iam_role           = aws_iam_role.ssm.name
  registration_limit = 1

  depends_on = [aws_iam_role_policy_attachment.ssm_core]

  lifecycle {
    ignore_changes = [expiration_date]
  }
}

# ── Lightsail Instance ────────────────────────────────────────────────────────

resource "aws_lightsail_instance" "this" {
  name              = local.name_prefix
  availability_zone = var.availability_zone
  blueprint_id      = var.blueprint_id
  bundle_id         = var.bundle_id

  user_data = <<-EOT
    #!/bin/bash
    set -euo pipefail
    amazon-ssm-agent -register -y \
      -id "${aws_ssm_activation.this.id}" \
      -code "${aws_ssm_activation.this.activation_code}" \
      -region "${data.aws_region.current.name}"
    systemctl restart amazon-ssm-agent
  EOT

  tags = local.tags
}

# ── Lightsail Firewall ────────────────────────────────────────────────────────

resource "aws_lightsail_instance_public_ports" "this" {
  instance_name = aws_lightsail_instance.this.name

  port_info {
    protocol  = "tcp"
    from_port = 22
    to_port   = 22
    cidrs     = ["0.0.0.0/0"]
  }

  port_info {
    protocol  = "tcp"
    from_port = 80
    to_port   = 80
    cidrs     = ["0.0.0.0/0"]
  }

  port_info {
    protocol  = "tcp"
    from_port = 443
    to_port   = 443
    cidrs     = ["0.0.0.0/0"]
  }
}
