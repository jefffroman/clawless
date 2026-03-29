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
        Action = ["cloudwatch:*", "logs:*", "sns:*", "budgets:*"]
        Resource = "*"
      },
      {
        # ECR, Step Functions, and Lambda — needed to refresh own resources during tofu apply
        Sid      = "SelfManaged"
        Effect   = "Allow"
        Action   = ["ecr:*", "events:*", "lambda:*", "states:*"]
        Resource = "*"
      },
      {
        # Item operations on lifecycle coordination table
        Sid    = "DynamoDBItems"
        Effect = "Allow"
        Action = [
          "dynamodb:Scan",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
          "dynamodb:GetItem",
        ]
        Resource = [aws_dynamodb_table.lifecycle_pending.arn]
      },
      {
        # Read-only table metadata — tofu refresh during targeted apply
        Sid    = "DynamoDBDescribe"
        Effect = "Allow"
        Action = [
          "dynamodb:DescribeTable",
          "dynamodb:DescribeContinuousBackups",
          "dynamodb:DescribeTimeToLive",
          "dynamodb:ListTagsOfResource",
        ]
        Resource = [
          aws_dynamodb_table.lifecycle_pending.arn,
          aws_dynamodb_table.wake_messages.arn,
        ]
      },
      {
        Sid      = "Bedrock"
        Effect   = "Allow"
        Action   = ["bedrock:GetInferenceProfile"]
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
  memory_size   = 2048

  ephemeral_storage {
    size = 1024 # MB — git clone + tofu working dir
  }

  environment {
    variables = {
      STATE_BUCKET    = local.state_bucket
      REPO_URL        = "https://github.com/jefffroman/clawless"
      LIFECYCLE_TABLE = aws_dynamodb_table.lifecycle_pending.name
      SNS_TOPIC_ARN   = aws_sns_topic.alerts.arn
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


# ── DynamoDB Lifecycle Table ──────────────────────────────────────────────────
# Single table for lifecycle coordination. One record per slug (last-write-wins).
#
# Fields:
#   slug (hash key)   — "client/agent" path
#   pending (S)       — SSM operation written by SFN: "Create", "Update", "Delete"
#   in_progress (BOOL)— ownership lock, written only by Lambda
#   timestamp (S)     — event time, used by Lambda to detect intent changes
#   ttl (N)           — 1-hour safety net for orphaned records
#
# Flow: SFN writes pending+timestamp via UpdateItem (preserves in_progress).
# Lambda grabs records by setting in_progress=true (conditional on false).
# One Lambda owns each slug until done — no cross-Lambda coordination.

resource "aws_dynamodb_table" "lifecycle_pending" {
  name         = "clawless-lifecycle-pending"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "slug"

  attribute {
    name = "slug"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  tags = var.tags
}

# ── Wake Messages Table ──────────────────────────────────────────────────────
# Stores pending wake messages keyed by agent slug. On resume, the wake-greet
# script reads and deletes its own entry. The write side (wake-listener Lambda)
# comes in a future phase; for now the table enables the read path so the
# wake-greet script is built once with the full DynamoDB check.
#
# Single item per slug. Schemaless attributes: message, channel, sender, timestamp.
# TTL auto-cleans stale messages (7-day default set by the writer).

resource "aws_dynamodb_table" "wake_messages" {
  name         = "clawless-wake-messages"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "slug"

  attribute {
    name = "slug"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  tags = var.tags
}

# ── Step Functions Express Workflow ───────────────────────────────────────────
# Four states: extract slug → write pending (UpdateItem, last-write-wins) →
# check if a Lambda already owns this slug → invoke Lambda if not.

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
        Action   = ["dynamodb:UpdateItem"]
        Resource = [aws_dynamodb_table.lifecycle_pending.arn]
      },
      {
        Sid    = "SSM"
        Effect = "Allow"
        Action = [
          "ssm:PutParameter",
          "ssm:DeleteParameter",
        ]
        Resource = ["arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.root.account_id}:parameter/clawless/clients/*"]
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
    Comment = "SSM write + pending lifecycle event + invoke Lambda if no owner"
    StartAt = "ExtractSlug"
    States = {
      ExtractSlug = {
        Type = "Pass"
        Parameters = {
          "slug.$" = "States.Format('{}/{}', States.ArrayGetItem(States.StringSplit($.name, '/'), 2), States.ArrayGetItem(States.StringSplit($.name, '/'), 3))"
        }
        ResultPath = "$.extract"
        Next       = "ChooseSSMAction"
      }
      ChooseSSMAction = {
        Type = "Choice"
        Choices = [
          {
            Variable     = "$.operation"
            StringEquals = "Delete"
            Next         = "DeleteSSM"
          },
          {
            # Update: SSM already written by the caller (e.g. clawless-sleep).
            # Skip WriteSSM and go straight to pending + Lambda invocation.
            Variable     = "$.operation"
            StringEquals = "Update"
            Next         = "WritePending"
          }
        ]
        Default = "WriteSSM"
      }
      WriteSSM = {
        Type     = "Task"
        Resource = "arn:aws:states:::aws-sdk:ssm:putParameter"
        Parameters = {
          "Name.$"    = "$.name"
          "Value.$"   = "$.ssm_value"
          "Type"      = "SecureString"
          "Overwrite" = true
        }
        ResultPath = "$.ssmResult"
        Next       = "WritePending"
      }
      DeleteSSM = {
        Type     = "Task"
        Resource = "arn:aws:states:::aws-sdk:ssm:deleteParameter"
        Parameters = {
          "Name.$" = "$.name"
        }
        ResultPath = "$.ssmResult"
        Next       = "WritePending"
      }
      WritePending = {
        Type     = "Task"
        Resource = "arn:aws:states:::dynamodb:updateItem"
        Parameters = {
          "TableName" = aws_dynamodb_table.lifecycle_pending.name
          "Key" = {
            "slug" = { "S.$" = "$.extract.slug" }
          }
          "UpdateExpression"          = "SET pending = :op, #ts = :ts, in_progress = if_not_exists(in_progress, :false)"
          "ExpressionAttributeNames"  = { "#ts" = "timestamp" }
          "ExpressionAttributeValues" = {
            ":op"    = { "S.$" = "$.operation" }
            ":ts"    = { "S.$" = "$.time" }
            ":false" = { "BOOL" = false }
          }
          "ReturnValues" = "ALL_NEW"
        }
        ResultPath = "$.dynamoResult"
        Next       = "CheckInProgress"
      }
      CheckInProgress = {
        Type = "Choice"
        Choices = [{
          Variable      = "$.dynamoResult.Attributes.in_progress.BOOL"
          BooleanEquals = true
          Next          = "AlreadyOwned"
        }]
        Default = "InvokeLambda"
      }
      AlreadyOwned = {
        Type    = "Succeed"
        Comment = "Another Lambda already owns this slug — skip invocation"
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
