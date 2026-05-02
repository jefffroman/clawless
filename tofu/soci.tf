# ── SOCI index-builder Lambda ────────────────────────────────────────────────
# Synchronously invoked by scripts/build-gateway-image.sh after pushing the
# gateway image with a candidate tag. Builds the SOCI lazy-load index and
# promotes :latest. Serverless so wake-time optimization doesn't drag an
# always-on VM along for the ride.

resource "aws_ecr_repository" "soci_builder" {
  name                 = "clawless-soci-index-builder"
  image_tag_mutability = "MUTABLE"
  force_delete         = false

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = var.tags
}

resource "aws_ecr_lifecycle_policy" "soci_builder" {
  repository = aws_ecr_repository.soci_builder.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep last 3 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 3
      }
      action = { type = "expire" }
    }]
  })
}

resource "null_resource" "soci_builder_image" {
  triggers = {
    dockerfile = filemd5("${path.module}/../lambda/soci-index-builder/Dockerfile")
    handler    = filemd5("${path.module}/../lambda/soci-index-builder/handler.py")
  }

  provisioner "local-exec" {
    command = "${path.module}/../scripts/build-soci-lambda-image.sh --region ${var.aws_region} --ecr-repo ${aws_ecr_repository.soci_builder.repository_url}"
  }

  depends_on = [aws_ecr_repository.soci_builder]
}

resource "aws_iam_role" "soci_builder" {
  name = "clawless-soci-index-builder"

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

resource "aws_iam_role_policy_attachment" "soci_builder_logs" {
  role       = aws_iam_role.soci_builder.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "soci_builder" {
  name = "clawless-soci-index-builder"
  role = aws_iam_role.soci_builder.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ECRAuth"
        Effect = "Allow"
        Action = [
          "ecr:GetAuthorizationToken",
        ]
        Resource = "*"
      },
      {
        Sid    = "ECRImageOps"
        Effect = "Allow"
        Action = [
          "ecr:BatchGetImage",
          "ecr:BatchCheckLayerAvailability",
          "ecr:GetDownloadUrlForLayer",
          "ecr:PutImage",
          "ecr:InitiateLayerUpload",
          "ecr:UploadLayerPart",
          "ecr:CompleteLayerUpload",
          "ecr:BatchDeleteImage",
          "ecr:DescribeImages",
        ]
        Resource = [aws_ecr_repository.gateway.arn]
      },
    ]
  })
}

resource "aws_lambda_function" "soci_builder" {
  function_name = "clawless-soci-index-builder"
  role          = aws_iam_role.soci_builder.arn
  architectures = ["arm64"]
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.soci_builder.repository_url}:latest"
  timeout       = 600 # 10 min — SOCI build for ~700MB image is typically 60-120s
  memory_size   = 3008

  # /tmp holds two OCI layouts: pre-SOCI pull (~700 MB) + post-SOCI convert
  # output (~750 MB). 8 GB gives ample headroom as the image grows.
  ephemeral_storage {
    size = 8192
  }

  lifecycle {
    ignore_changes = [image_uri]
  }

  depends_on = [null_resource.soci_builder_image]

  tags = var.tags
}

resource "aws_cloudwatch_log_group" "soci_builder" {
  name              = "/aws/lambda/clawless-soci-index-builder"
  retention_in_days = 14
  tags              = var.tags
}
