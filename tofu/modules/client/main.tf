# ── Per-client Fargate gateway ───────────────────────────────────────────────
# Single task per client; desired_count flipped 0↔1 for sleep/wake by the
# lifecycle Lambda. No scaling policies — each client is one task.

locals {
  # ECS service names can't contain slashes; "client/agent" → "client-agent".
  slug_safe = replace(var.agent_slug, "/", "-")
  name      = "clawless-${local.slug_safe}"
}

# Task role: the SDK inside the gateway container uses this to reach Bedrock,
# the backup S3 bucket, and (so the gateway can self-stop on sleep) the
# client's own ECS service.
resource "aws_iam_role" "task" {
  name = local.name

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

resource "aws_iam_role_policy" "task" {
  name = "clawless-fargate-client"
  role = aws_iam_role.task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
          "bedrock:Converse",
          "bedrock:ConverseStream",
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "s3:ListBucket",
          "s3:GetBucketLocation",
        ]
        Resource = "arn:aws:s3:::${var.backup_bucket}"
        Condition = {
          StringLike = { "s3:prefix" = ["agents/${var.agent_slug}/*"] }
        }
      },
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
        ]
        Resource = "arn:aws:s3:::${var.backup_bucket}/agents/${var.agent_slug}/*"
      },
      {
        Effect   = "Allow"
        Action   = ["ecs:UpdateService", "ecs:DescribeServices"]
        Resource = "arn:aws:ecs:${var.aws_region}:*:service/${var.cluster_name}/${local.name}"
      },
      {
        Effect = "Allow"
        Action = ["ssm:PutParameter"]
        Resource = "arn:aws:ssm:${var.aws_region}:*:parameter/clawless/clients/${var.agent_slug}/active"
      },
      {
        Effect   = "Allow"
        Action   = ["states:StartExecution"]
        Resource = var.lifecycle_sfn_arn
      },
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:DeleteItem",
        ]
        Resource = var.wake_messages_table_arn != "" ? var.wake_messages_table_arn : "*"
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "task" {
  name              = "/clawless/fargate/${local.slug_safe}"
  retention_in_days = 14
  tags              = var.tags
}

resource "random_password" "gateway_token" {
  length  = 48
  special = false
}

resource "aws_ssm_parameter" "gateway_token" {
  name  = "/clawless/clients/${var.agent_slug}/gateway_token"
  type  = "SecureString"
  value = random_password.gateway_token.result

  lifecycle {
    ignore_changes = [value]
  }

  tags = var.tags
}

resource "aws_ecs_task_definition" "gateway" {
  family                   = local.name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }
  execution_role_arn = var.execution_role_arn
  task_role_arn      = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name      = "gateway"
    image     = var.image_uri
    essential = true

    environment = [
      { name = "AGENT_SLUG", value = var.agent_slug },
      { name = "AGENT_NAME", value = var.agent_name != "" ? var.agent_name : local.slug_safe },
      { name = "BACKUP_BUCKET", value = var.backup_bucket },
      { name = "AWS_DEFAULT_REGION", value = var.aws_region },
      { name = "ECS_CLUSTER", value = var.cluster_name },
      { name = "OPENCLAW_MODEL", value = var.bedrock_model },
      { name = "OPENCLAW_CHANNEL", value = var.agent_channel },
      {
        name  = "OPENCLAW_CHANNEL_CONFIG"
        value = var.channel_config == null ? "" : jsonencode(var.channel_config)
      },
      { name = "LIFECYCLE_SFN_ARN", value = var.lifecycle_sfn_arn },
      { name = "WAKE_LISTENER_URL", value = var.wake_listener_url },
      { name = "WAKE_MESSAGES_TABLE", value = var.wake_messages_table_name },
      { name = "SEARXNG_URL", value = var.searxng_url },
    ]

    secrets = [
      {
        name      = "OPENCLAW_GATEWAY_TOKEN"
        valueFrom = aws_ssm_parameter.gateway_token.arn
      },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.task.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "gateway"
      }
    }

    stopTimeout = 30
  }])

  tags = var.tags
}

resource "aws_ecs_service" "gateway" {
  name            = local.name
  cluster         = var.cluster_arn
  task_definition = aws_ecs_task_definition.gateway.arn
  launch_type     = "FARGATE"
  desired_count   = var.active ? 1 : 0

  network_configuration {
    subnets          = var.subnet_ids
    security_groups  = var.security_group_ids
    assign_public_ip = var.assign_public_ip
  }

  # The lifecycle Lambda flips desired_count between 0 and 1 via ECS API,
  # so don't let Tofu fight it on apply.
  lifecycle {
    ignore_changes = [desired_count]
  }

  # First boot needs the seed files in place before the task starts or the
  # entrypoint's sync_down finds an empty prefix and bails. Subsequent applies
  # no-op on the seed objects (lifecycle ignore_changes) so this is cheap.
  depends_on = [
    aws_s3_object.seed_md,
    aws_s3_object.seed_gitignore,
  ]

  tags = var.tags
}
