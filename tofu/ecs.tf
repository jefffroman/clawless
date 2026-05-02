# ── Clawless ECS cluster + shared task execution role ────────────────────────
# One Fargate cluster holds every client's gateway service. Zero cost at rest
# (Fargate = no EC2 instances); cost scales with running tasks only.

resource "aws_ecs_cluster" "clawless" {
  name = "clawless"

  setting {
    name  = "containerInsights"
    value = "disabled"
  }

  tags = var.tags
}

resource "aws_ecs_cluster_capacity_providers" "clawless" {
  cluster_name       = aws_ecs_cluster.clawless.name
  capacity_providers = ["FARGATE"]

  default_capacity_provider_strategy {
    base              = 0
    weight            = 1
    capacity_provider = "FARGATE"
  }
}

# Task execution role: shared across all clients. Used by the ECS agent to
# pull the image from ECR and push logs to CloudWatch. This is NOT the task
# role (which the container's SDK uses) — that's per-client, created inside
# the client-fargate module.
resource "aws_iam_role" "fargate_execution" {
  name = "clawless-fargate-execution"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "fargate_execution_managed" {
  role       = aws_iam_role.fargate_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}
