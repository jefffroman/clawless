# ── Clawless Fargate gateway container image ─────────────────────────────────
# ECR repo + build-on-change trigger for the per-client Fargate gateway.
# Mirrors the `clawless-lifecycle` pattern in lambda.tf.

resource "aws_ecr_repository" "gateway" {
  name                 = "clawless-gateway"
  image_tag_mutability = "MUTABLE"
  force_delete         = false

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = var.tags
}

resource "aws_ecr_lifecycle_policy" "gateway" {
  repository = aws_ecr_repository.gateway.name

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

resource "null_resource" "gateway_image" {
  triggers = {
    dockerfile    = filemd5("${path.module}/../docker/gateway/Dockerfile")
    entrypoint    = filemd5("${path.module}/../docker/gateway/entrypoint.sh")
    configurator  = filemd5("${path.module}/../docker/gateway/files/configure_openclaw.py")
    indexer       = filemd5("${path.module}/../docker/gateway/files/indexer.py")
    search        = filemd5("${path.module}/../docker/gateway/files/search.py")
    auto_retrieve = filemd5("${path.module}/../docker/gateway/files/auto_retrieve.py")
  }

  provisioner "local-exec" {
    command = "${path.module}/../scripts/build-gateway-image.sh --region ${var.aws_region} --ecr-repo ${aws_ecr_repository.gateway.repository_url}"
  }

  depends_on = [aws_ecr_repository.gateway]
}
