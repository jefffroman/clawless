# ── Locals ────────────────────────────────────────────────────────────────────

data "aws_caller_identity" "root" {}

locals {
  state_bucket = "clawless-tfstate-${data.aws_caller_identity.root.account_id}"
}

# ── ECR Repository ────────────────────────────────────────────────────────────

resource "aws_ecr_repository" "lifecycle" {
  name                 = "clawless-lifecycle"
  image_tag_mutability = "MUTABLE"
  force_delete         = false

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = var.tags
}

resource "aws_ecr_lifecycle_policy" "lifecycle" {
  repository = aws_ecr_repository.lifecycle.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep last 5 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 5
      }
      action = { type = "expire" }
    }]
  })
}

# ── Build and push container image ────────────────────────────────────────────
# Runs build-lambda.sh whenever Dockerfile or handler.py changes.
# Ensures the image exists before the Lambda function is created/updated.

resource "null_resource" "lambda_image" {
  triggers = {
    dockerfile = filemd5("${path.root}/../lambda/Dockerfile")
    handler    = filemd5("${path.root}/../lambda/handler.py")
  }

  provisioner "local-exec" {
    command = "${path.root}/../scripts/build-lambda.sh --region ${var.aws_region} --ecr-repo ${aws_ecr_repository.lifecycle.repository_url}"
  }

  depends_on = [aws_ecr_repository.lifecycle]
}

# ── IAM Role ──────────────────────────────────────────────────────────────────

resource "aws_iam_role" "lifecycle_lambda" {
  name = "clawless-lifecycle-lambda"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "lifecycle_lambda_logs" {
  role       = aws_iam_role.lifecycle_lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "lifecycle_lambda" {
  name = "clawless-lifecycle-lambda"
  role = aws_iam_role.lifecycle_lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "StateBackend"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject",
        "s3:ListBucket", "s3:GetBucketVersioning"]
        Resource = [
          "arn:aws:s3:::${local.state_bucket}",
          "arn:aws:s3:::${local.state_bucket}/*",
        ]
      },
      {
        # Full S3 access for managing per-client workspace backup buckets
        Sid      = "S3Client"
        Effect   = "Allow"
        Action   = ["s3:*"]
        Resource = "*"
      },
      {
        Sid    = "SSM"
        Effect = "Allow"
        Action = ["ssm:*"]
        Resource = "*"
      },
      {
        Sid      = "Lightsail"
        Effect   = "Allow"
        Action   = ["lightsail:*"]
        Resource = "*"
      },
      {
        # IAM role and policy management for per-client SSM roles
        Sid      = "IAM"
        Effect   = "Allow"
        Action   = ["iam:*"]
        Resource = "*"
      },
      {
        Sid      = "Monitoring"
        Effect   = "Allow"
        Action   = ["cloudwatch:*", "logs:CreateLogGroup",
          "logs:CreateLogStream", "logs:PutLogEvents",
        "sns:*", "budgets:*"]
        Resource = "*"
      },
      {
        # ECR, EventBridge, and Lambda — needed to refresh own resources during tofu apply
        Sid      = "SelfManaged"
        Effect   = "Allow"
        Action   = ["ecr:*", "events:*", "lambda:*"]
        Resource = "*"
      },
    ]
  })
}

# ── Lambda Function ───────────────────────────────────────────────────────────

resource "aws_lambda_function" "lifecycle" {
  function_name = "clawless-lifecycle"
  role          = aws_iam_role.lifecycle_lambda.arn
  architectures = ["arm64"]
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.lifecycle.repository_url}:latest"
  timeout       = 900 # 15 min — tofu apply for a client takes 1-3 min
  memory_size   = 1024

  ephemeral_storage {
    size = 1024 # MB — git clone + tofu working dir
  }

  environment {
    variables = {
      STATE_BUCKET = local.state_bucket
      REPO_URL     = "https://github.com/jefffroman/clawless"
    }
  }

  # Image URI is managed by build-lambda.sh / null_resource.lambda_image,
  # not by tofu, so changes here don't overwrite the latest pushed image.
  lifecycle {
    ignore_changes = [image_uri]
  }

  depends_on = [null_resource.lambda_image]

  tags = var.tags
}

# ── Backup Lambda ─────────────────────────────────────────────────────────────
# Same image as lifecycle Lambda; uses the backup_handler entry point.

resource "aws_lambda_function" "backup" {
  function_name = "clawless-backup"
  role          = aws_iam_role.lifecycle_lambda.arn
  architectures = ["arm64"]
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.lifecycle.repository_url}:latest"
  timeout       = 300
  memory_size   = 512

  image_config {
    command = ["handler.backup_handler"]
  }

  lifecycle {
    ignore_changes = [image_uri]
  }

  depends_on = [null_resource.lambda_image]

  tags = var.tags
}

resource "aws_cloudwatch_event_rule" "nightly_backup" {
  name                = "clawless-nightly-backup"
  description         = "Copy each active client's backup bucket to the shared archive"
  schedule_expression = "cron(0 7 * * ? *)" # 3 AM EDT (UTC-4)

  tags = var.tags
}

resource "aws_cloudwatch_event_target" "backup_lambda" {
  rule      = aws_cloudwatch_event_rule.nightly_backup.name
  target_id = "clawless-backup"
  arn       = aws_lambda_function.backup.arn
}

resource "aws_lambda_permission" "eventbridge_backup" {
  statement_id  = "AllowEventBridgeBackup"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.backup.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.nightly_backup.arn
}

# ── EventBridge Rule ──────────────────────────────────────────────────────────
# Fires on any Create/Update to /clawless/clients — covers add, remove,
# pause, and resume (all write to this parameter via their respective scripts).

resource "aws_cloudwatch_event_rule" "clients_change" {
  name        = "clawless-clients-change"
  description = "Trigger lifecycle Lambda on /clawless/clients SSM changes"

  event_pattern = jsonencode({
    source      = ["aws.ssm"]
    "detail-type" = ["Parameter Store Change"]
    detail = {
      name      = ["/clawless/clients"]
      operation = ["Update", "Create"]
    }
  })

  tags = var.tags
}

resource "aws_cloudwatch_event_target" "lifecycle_lambda" {
  rule      = aws_cloudwatch_event_rule.clients_change.name
  target_id = "clawless-lifecycle"
  arn       = aws_lambda_function.lifecycle.arn
}

resource "aws_lambda_permission" "eventbridge" {
  statement_id  = "AllowEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.lifecycle.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.clients_change.arn
}
