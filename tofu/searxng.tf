# ── Clawless SearXNG Lambda ──────────────────────────────────────────────────
# Shared SearXNG service for all clawless agents. Container Lambda running
# SearXNG's Flask webapp via the AWS Lambda Web Adapter. Function URL is
# unauthenticated (SearXNG is a public API target anyway, but cold-start and
# Lambda concurrency provide natural rate limiting).

resource "aws_ecr_repository" "searxng" {
  name                 = "clawless-searxng"
  image_tag_mutability = "MUTABLE"
  force_delete         = false

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = var.tags
}

resource "aws_ecr_lifecycle_policy" "searxng" {
  repository = aws_ecr_repository.searxng.name

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

resource "null_resource" "searxng_image" {
  triggers = {
    dockerfile = filemd5("${path.module}/../lambda/searxng/Dockerfile")
    settings   = filemd5("${path.module}/../lambda/searxng/settings.yml")
  }

  provisioner "local-exec" {
    command = "${path.module}/../scripts/build-searxng-image.sh --region ${var.aws_region} --ecr-repo ${aws_ecr_repository.searxng.repository_url}"
  }

  depends_on = [aws_ecr_repository.searxng]
}

resource "aws_iam_role" "searxng" {
  name = "clawless-searxng"

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

resource "aws_iam_role_policy_attachment" "searxng_logs" {
  role       = aws_iam_role.searxng.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_lambda_function" "searxng" {
  function_name = "clawless-searxng"
  role          = aws_iam_role.searxng.arn
  architectures = ["arm64"]
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.searxng.repository_url}:latest"
  timeout       = 30
  memory_size   = 1024

  lifecycle {
    ignore_changes = [image_uri]
  }

  depends_on = [null_resource.searxng_image]

  tags = var.tags
}

resource "aws_lambda_function_url" "searxng" {
  function_name      = aws_lambda_function.searxng.function_name
  authorization_type = "NONE"
}

output "searxng_url" {
  value = aws_lambda_function_url.searxng.function_url
}
