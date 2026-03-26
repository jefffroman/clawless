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
        Sid      = "SSM"
        Effect   = "Allow"
        Action   = ["ssm:*"]
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
        Sid    = "Monitoring"
        Effect = "Allow"
        Action = ["cloudwatch:*", "logs:CreateLogGroup",
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
        Sid    = "DynamoDB"
        Effect = "Allow"
        Action = [
          "dynamodb:Scan",
          "dynamodb:DeleteItem",
        ]
        Resource = [aws_dynamodb_table.lifecycle_events.arn]
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
  memory_size   = 2048

  ephemeral_storage {
    size = 1024 # MB — git clone + tofu working dir
  }

  environment {
    variables = {
      STATE_BUCKET  = local.state_bucket
      REPO_URL      = "https://github.com/jefffroman/clawless"
      EVENTS_TABLE  = aws_dynamodb_table.lifecycle_events.name
      SNS_TOPIC_ARN = aws_sns_topic.alerts.arn
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
# Fires on any Create/Update/Delete to agent-level parameters under
# /clawless/clients/*/*. Targets the Step Functions workflow, which writes the
# event to DynamoDB then invokes the lifecycle Lambda.

resource "aws_cloudwatch_event_rule" "clients_change" {
  name        = "clawless-clients-change"
  description = "Trigger lifecycle Lambda on /clawless/clients/* SSM changes"

  event_pattern = jsonencode({
    source        = ["aws.ssm"]
    "detail-type" = ["Parameter Store Change"]
    detail = {
      name      = [{ wildcard = "/clawless/clients/*/*" }]
      operation = ["Update", "Create", "Delete"]
    }
  })

  tags = var.tags
}

# ── DynamoDB Event Queue ──────────────────────────────────────────────────────
# Events flow: EventBridge → Step Functions → DynamoDB PutItem + Lambda invoke.
# Step Functions guarantees the event is durably written before Lambda starts.
# Lambda atomically grabs events via DeleteItem (ReturnValues=ALL_OLD) — only
# one invocation gets each event. No coordination lock needed.

resource "aws_dynamodb_table" "lifecycle_events" {
  name         = "clawless-lifecycle-events"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "event_id"

  attribute {
    name = "event_id"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  tags = var.tags
}

# ── Step Functions Express Workflow ───────────────────────────────────────────
# Two sequential steps: write event to DynamoDB, then invoke lifecycle Lambda.

resource "aws_iam_role" "lifecycle_sfn" {
  name = "clawless-lifecycle-sfn"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "states.amazonaws.com" }
    }]
  })

  tags = var.tags
}

resource "aws_iam_role_policy" "lifecycle_sfn" {
  name = "clawless-lifecycle-sfn"
  role = aws_iam_role.lifecycle_sfn.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "DynamoDB"
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem"]
        Resource = [aws_dynamodb_table.lifecycle_events.arn]
      },
      {
        Sid      = "Lambda"
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction"]
        Resource = [aws_lambda_function.lifecycle.arn]
      },
      {
        Sid    = "Logs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogDelivery",
          "logs:GetLogDelivery",
          "logs:UpdateLogDelivery",
          "logs:DeleteLogDelivery",
          "logs:ListLogDeliveries",
          "logs:PutResourcePolicy",
          "logs:DescribeResourcePolicies",
          "logs:DescribeLogGroups",
          "logs:PutLogEvents",
          "logs:CreateLogStream",
        ]
        Resource = ["*"]
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "lifecycle_sfn" {
  name              = "/aws/vendedlogs/states/clawless-lifecycle"
  retention_in_days = 14
  tags              = var.tags
}

resource "aws_sfn_state_machine" "lifecycle" {
  name     = "clawless-lifecycle"
  role_arn = aws_iam_role.lifecycle_sfn.arn
  type     = "EXPRESS"

  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.lifecycle_sfn.arn}:*"
    include_execution_data = true
    level                  = "ALL"
  }

  definition = jsonencode({
    Comment = "Write lifecycle event to DynamoDB then invoke Lambda"
    StartAt = "ExtractSlug"
    States = {
      ExtractSlug = {
        Type = "Pass"
        Parameters = {
          "event_id.$"  = "$.event_id"
          "operation.$" = "$.operation"
          "time.$"      = "$.time"
          "name.$"      = "$.name"
          "slug.$"      = "States.Format('{}/{}', States.ArrayGetItem(States.StringSplit($.name, '/'), 2), States.ArrayGetItem(States.StringSplit($.name, '/'), 3))"
        }
        Next = "WriteEvent"
      }
      WriteEvent = {
        Type     = "Task"
        Resource = "arn:aws:states:::dynamodb:putItem"
        Parameters = {
          "TableName" = aws_dynamodb_table.lifecycle_events.name
          "Item" = {
            "event_id"  = { "S.$" = "$.event_id" }
            "slug"      = { "S.$" = "$.slug" }
            "operation" = { "S.$" = "$.operation" }
            "timestamp" = { "S.$" = "$.time" }
          }
        }
        ResultPath = "$.dynamoResult"
        Next       = "InvokeLambda"
      }
      InvokeLambda = {
        Type     = "Task"
        Resource = "arn:aws:states:::aws-sdk:lambda:invoke"
        Parameters = {
          "FunctionName"   = aws_lambda_function.lifecycle.arn
          "InvocationType" = "Event"
          "Payload"        = "{\"source\":\"step-functions\"}"
        }
        End = true
      }
    }
  })

  tags = var.tags
}

# ── EventBridge → Step Functions ─────────────────────────────────────────────

resource "aws_iam_role" "eventbridge_sfn" {
  name = "clawless-eventbridge-sfn"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "events.amazonaws.com" }
    }]
  })

  tags = var.tags
}

resource "aws_iam_role_policy" "eventbridge_sfn" {
  name = "clawless-eventbridge-sfn"
  role = aws_iam_role.eventbridge_sfn.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["states:StartExecution"]
      Resource = [aws_sfn_state_machine.lifecycle.arn]
    }]
  })
}

resource "aws_cloudwatch_event_target" "lifecycle_sfn" {
  rule      = aws_cloudwatch_event_rule.clients_change.name
  target_id = "clawless-lifecycle-sfn"
  arn       = aws_sfn_state_machine.lifecycle.arn
  role_arn  = aws_iam_role.eventbridge_sfn.arn

  input_transformer {
    input_paths = {
      id        = "$.id"
      time      = "$.time"
      name      = "$.detail.name"
      operation = "$.detail.operation"
    }
    input_template = <<-EOT
      {"event_id": <id>, "time": <time>, "name": <name>, "operation": <operation>}
    EOT
  }

  dead_letter_config {
    arn = aws_sqs_queue.eventbridge_dlq.arn
  }

  retry_policy {
    maximum_event_age_in_seconds = 86400
    maximum_retry_attempts       = 185
  }
}

# ── EventBridge DLQ ──────────────────────────────────────────────────────────
# Catches events that exhaust all EventBridge retries (185 over 24h).

resource "aws_sqs_queue" "eventbridge_dlq" {
  name                      = "clawless-eventbridge-dlq"
  message_retention_seconds = 1209600 # 14 days
  tags                      = var.tags
}

resource "aws_sqs_queue_policy" "eventbridge_dlq" {
  queue_url = aws_sqs_queue.eventbridge_dlq.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.eventbridge_dlq.arn
    }]
  })
}
