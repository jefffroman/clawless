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
    dockerfile = filemd5("${path.module}/../lambda/Dockerfile")
    handler    = filemd5("${path.module}/../lambda/handler.py")
  }

  provisioner "local-exec" {
    command = "${path.module}/../scripts/build-lambda.sh --region ${var.aws_region} --ecr-repo ${aws_ecr_repository.lifecycle.repository_url}"
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
      {
        Sid    = "SQS"
        Effect = "Allow"
        Action = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:DeleteMessageBatch",
          "sqs:GetQueueAttributes",
          "sqs:GetQueueUrl",
        ]
        Resource = [aws_sqs_queue.lifecycle.arn]
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
      STATE_BUCKET   = local.state_bucket
      REPO_URL       = "https://github.com/jefffroman/clawless"
      SQS_QUEUE_URL  = aws_sqs_queue.lifecycle.url
      SNS_TOPIC_ARN  = aws_sns_topic.alerts.arn
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

# ── EventBridge Rule ──────────────────────────────────────────────────────────
# Fires on any Create/Update to any parameter under /clawless/clients/ —
# covers agent add, remove, pause, and resume (client and agent records).

resource "aws_cloudwatch_event_rule" "clients_change" {
  name        = "clawless-clients-change"
  description = "Trigger lifecycle Lambda on /clawless/clients/* SSM changes"

  event_pattern = jsonencode({
    source        = ["aws.ssm"]
    "detail-type" = ["Parameter Store Change"]
    detail = {
      name      = [{ prefix = "/clawless/clients/" }]
      operation = ["Update", "Create", "Delete"]
    }
  })

  tags = var.tags
}

# ── SQS Queue ──────────────────────────────────────────────────────────────────
# Events flow: EventBridge → SQS → Lambda (concurrency=1).
# Lambda drains the queue fully before processing, deduplicates slugs, and runs
# a single batched tofu apply. This eliminates concurrent invocations and state
# lock contention.

resource "aws_sqs_queue" "lifecycle" {
  name                       = "clawless-lifecycle"
  visibility_timeout_seconds = 960 # Lambda timeout (900s) + 60s buffer
  message_retention_seconds  = 345600 # 4 days
  tags                       = var.tags
}

resource "aws_sqs_queue" "lifecycle_dlq" {
  name                      = "clawless-lifecycle-dlq"
  message_retention_seconds = 1209600 # 14 days
  tags                      = var.tags
}

resource "aws_sqs_queue_redrive_policy" "lifecycle" {
  queue_url = aws_sqs_queue.lifecycle.id
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.lifecycle_dlq.arn
    maxReceiveCount     = 3
  })
}

resource "aws_sqs_queue_policy" "lifecycle" {
  queue_url = aws_sqs_queue.lifecycle.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.lifecycle.arn
      Condition = {
        ArnEquals = { "aws:SourceArn" = aws_cloudwatch_event_rule.clients_change.arn }
      }
    }]
  })
}

# EventBridge → SQS (replaces direct Lambda invocation)
resource "aws_cloudwatch_event_target" "lifecycle_sqs" {
  rule      = aws_cloudwatch_event_rule.clients_change.name
  target_id = "clawless-lifecycle-queue"
  arn       = aws_sqs_queue.lifecycle.arn
}

# SQS → Lambda event source mapping (triggers Lambda, which then drains the rest)
resource "aws_lambda_event_source_mapping" "lifecycle_sqs" {
  event_source_arn                   = aws_sqs_queue.lifecycle.arn
  function_name                      = aws_lambda_function.lifecycle.arn
  batch_size                         = 10
  maximum_batching_window_in_seconds = 0 # Invoke immediately — wakes must be fast
  function_response_types            = ["ReportBatchItemFailures"]
  enabled                            = true

  scaling_config {
    maximum_concurrency = 2 # Cap at 1 active + 1 warming; no account reservation needed
  }
}
