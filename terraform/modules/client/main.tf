locals {
  name_prefix = "clawless-${var.client_slug}"
  tags = merge(var.tags, {
    Client = var.client_slug
  })
}

data "aws_region" "current" {}

# ── IAM Role (SSM trust) ──────────────────────────────────────────────────────
# Lightsail has no native instance profile support, so we use SSM Hybrid
# Activation. The instance registers with SSM on first boot via user_data;
# SSM then issues rotating 1-hour temporary credentials automatically.
# No long-lived access keys are created or stored anywhere.

resource "aws_iam_role" "ssm" {
  name = "${local.name_prefix}-ssm"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ssm.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = local.tags
}

resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.ssm.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "bedrock" {
  name = "bedrock-invoke"
  role = aws_iam_role.ssm.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "BedrockInvokeModel"
      Effect = "Allow"
      Action = [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
      ]
      Resource = "*"
    }]
  })
}

# ── SSM Activation ────────────────────────────────────────────────────────────
# One-time activation credentials passed to the instance via user_data.
# After the instance registers, SSM manages credential rotation automatically.
# expiration_date is ignored after creation to prevent plan churn.

resource "aws_ssm_activation" "this" {
  name               = local.name_prefix
  iam_role           = aws_iam_role.ssm.name
  registration_limit = 1

  depends_on = [aws_iam_role_policy_attachment.ssm_core]

  lifecycle {
    ignore_changes = [expiration_date]
  }
}

# ── Lightsail Instance ────────────────────────────────────────────────────────

resource "aws_lightsail_instance" "this" {
  name              = local.name_prefix
  availability_zone = var.availability_zone
  blueprint_id      = var.blueprint_id
  bundle_id         = var.bundle_id

  # Registers the instance with SSM on first boot.
  # SSM agent is pre-installed on Ubuntu-based Lightsail blueprints.
  # After registration, the AWS SDK on the instance picks up rotating
  # temporary credentials automatically via the SSM credential provider.
  user_data = <<-EOT
    #!/bin/bash
    set -euo pipefail
    amazon-ssm-agent -register -y \
      -id "${aws_ssm_activation.this.id}" \
      -code "${aws_ssm_activation.this.activation_code}" \
      -region "${data.aws_region.current.name}"
    systemctl restart amazon-ssm-agent
  EOT

  tags = local.tags
}

# ── Lightsail Firewall ────────────────────────────────────────────────────────
# Allowlist-only: port 18789 (OpenClaw gateway) is intentionally absent.

resource "aws_lightsail_instance_public_ports" "this" {
  instance_name = aws_lightsail_instance.this.name

  port_info {
    protocol  = "tcp"
    from_port = 22
    to_port   = 22
    cidrs     = ["0.0.0.0/0"]
  }

  port_info {
    protocol  = "tcp"
    from_port = 80
    to_port   = 80
    cidrs     = ["0.0.0.0/0"]
  }

  port_info {
    protocol  = "tcp"
    from_port = 443
    to_port   = 443
    cidrs     = ["0.0.0.0/0"]
  }
}
