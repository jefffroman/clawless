# ── Shared archive bucket for removed clients ─────────────────────────────────
# When a client is removed, the Lambda copies its backup bucket contents here
# before tofu destroys it. Objects expire after 90 days (3 noncurrent versions).

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
    id     = "expire-archived-backups"
    status = "Enabled"

    filter {}

    expiration {
      days = 90
    }

    noncurrent_version_expiration {
      newer_noncurrent_versions = 2
      noncurrent_days           = 90
    }
  }

  depends_on = [aws_s3_bucket_versioning.backups]
}
