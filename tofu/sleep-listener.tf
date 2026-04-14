# ── Clawless sleep-listener Lambda ───────────────────────────────────────────
# Zip-packaged Python Lambda behind a Function URL. The gateway calls it with
# a shared-secret header to set its own ECS service desiredCount=0.

resource "aws_ssm_parameter" "sleep_listener_token" {
  name        = "/clawless/sleep-listener/token"
  type        = "SecureString"
  value       = "placeholder-rotate-me"
  description = "Shared secret for the sleep-listener Lambda auth header."

  lifecycle {
    ignore_changes = [value]
  }

  tags = var.tags
}

data "archive_file" "sleep_listener" {
  type        = "zip"
  source_file = "${path.module}/../lambda/sleep_listener/handler.py"
  output_path = "${path.module}/.build/sleep_listener.zip"
}

resource "aws_iam_role" "sleep_listener" {
  name = "clawless-sleep-listener"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "sleep_listener_logs" {
  role       = aws_iam_role.sleep_listener.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "sleep_listener" {
  name = "sleep-listener"
  role = aws_iam_role.sleep_listener.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ecs:UpdateService", "ecs:DescribeServices"]
        Resource = "arn:aws:ecs:${var.aws_region}:*:service/${aws_ecs_cluster.clawless.name}/*"
      },
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = aws_ssm_parameter.sleep_listener_token.arn
      },
    ]
  })
}

resource "aws_lambda_function" "sleep_listener" {
  function_name    = "clawless-sleep-listener"
  role             = aws_iam_role.sleep_listener.arn
  runtime          = "python3.11"
  handler          = "handler.lambda_handler"
  architectures    = ["arm64"]
  filename         = data.archive_file.sleep_listener.output_path
  source_code_hash = data.archive_file.sleep_listener.output_base64sha256
  timeout          = 10
  memory_size      = 256

  environment {
    variables = {
      ECS_CLUSTER    = aws_ecs_cluster.clawless.name
      AUTH_SSM_PARAM = aws_ssm_parameter.sleep_listener_token.name
    }
  }

  tags = var.tags
}

resource "aws_lambda_function_url" "sleep_listener" {
  function_name      = aws_lambda_function.sleep_listener.function_name
  authorization_type = "NONE"
}

output "sleep_listener_url" {
  value = aws_lambda_function_url.sleep_listener.function_url
}
