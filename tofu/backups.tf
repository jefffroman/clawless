# ── Shared backup bucket ──────────────────────────────────────────────────────
# Active clients sync their workspace here hourly (clients/{slug}/workspace/).
# When a client is removed, the Lambda copies its data here before destroying it.
# Lifecycle: 3 rotating versions — current kept indefinitely, 2 noncurrent kept ≥7 days.

resource "aws_s3_bucket" "backups" {
  bucket = "clawless-backups-${data.aws_caller_identity.root.account_id}"
  tags   = var.tags
}

resource "aws_s3_bucket_public_access_block" "backups" {
  bucket                  = aws_s3_bucket.backups.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "backups" {
  bucket = aws_s3_bucket.backups.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id

  rule {
    id     = "rotate-backups"
    status = "Enabled"

    filter {}

    expiration {
      expired_object_delete_marker = true
    }

    noncurrent_version_expiration {
      newer_noncurrent_versions = 2
      noncurrent_days           = 7
    }
  }

  depends_on = [aws_s3_bucket_versioning.backups]
}

# ── Replica bucket (us-east-2 / Ohio) ─────────────────────────────────────────

resource "aws_s3_bucket" "backup_replica" {
  provider = aws.replica
  bucket   = "clawless-backups-replica-${data.aws_caller_identity.root.account_id}"
  tags     = var.tags
}

resource "aws_s3_bucket_public_access_block" "backup_replica" {
  provider                = aws.replica
  bucket                  = aws_s3_bucket.backup_replica.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "backup_replica" {
  provider = aws.replica
  bucket   = aws_s3_bucket.backup_replica.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "backup_replica" {
  provider = aws.replica
  bucket   = aws_s3_bucket.backup_replica.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# ── CRR IAM Role ──────────────────────────────────────────────────────────────

resource "aws_iam_role" "backup_replication" {
  name = "clawless-backup-replication"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "s3.amazonaws.com" }
    }]
  })

  tags = var.tags
}

resource "aws_iam_role_policy" "backup_replication" {
  name = "clawless-backup-replication"
  role = aws_iam_role.backup_replication.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetReplicationConfiguration", "s3:ListBucket"]
        Resource = aws_s3_bucket.backups.arn
      },
      {
        Effect = "Allow"
        Action = [
          "s3:GetObjectVersionForReplication",
          "s3:GetObjectVersionAcl",
          "s3:GetObjectVersionTagging",
        ]
        Resource = "${aws_s3_bucket.backups.arn}/*"
      },
      {
        Effect = "Allow"
        Action = [
          "s3:ReplicateObject",
          "s3:ReplicateDelete",
          "s3:ReplicateTags",
        ]
        Resource = "${aws_s3_bucket.backup_replica.arn}/*"
      },
    ]
  })
}

# ── CRR Configuration ─────────────────────────────────────────────────────────

resource "aws_s3_bucket_replication_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id
  role   = aws_iam_role.backup_replication.arn

  rule {
    id     = "replicate-all"
    status = "Enabled"

    filter {}

    destination {
      bucket        = aws_s3_bucket.backup_replica.arn
      storage_class = "STANDARD_IA"
    }

    delete_marker_replication {
      status = "Enabled"
    }
  }

  depends_on = [aws_s3_bucket_versioning.backups, aws_s3_bucket_versioning.backup_replica]
}
