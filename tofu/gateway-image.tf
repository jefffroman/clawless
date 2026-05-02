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
    dockerfile = filemd5("${path.module}/../docker/gateway/Dockerfile")
    entrypoint = filemd5("${path.module}/../docker/gateway/entrypoint.sh")
    # Hash every .py + .md under docker/gateway/app/ so any application code
    # change rebuilds the image. dirsha256 doesn't exist in tofu, so we
    # concatenate file hashes and sha1 the result.
    app_dir = sha1(join("", [
      for f in fileset("${path.module}/../docker/gateway/app", "**") :
      filesha1("${path.module}/../docker/gateway/app/${f}")
    ]))
  }

  provisioner "local-exec" {
    command = "${path.module}/../scripts/build-gateway-image.sh --region ${var.aws_region} --ecr-repo ${aws_ecr_repository.gateway.repository_url}"
  }

  # build-gateway-image.sh invokes the SOCI Lambda synchronously, so the
  # Lambda must exist and be ready before this resource runs.
  depends_on = [
    aws_ecr_repository.gateway,
    aws_lambda_function.soci_builder,
  ]
}
