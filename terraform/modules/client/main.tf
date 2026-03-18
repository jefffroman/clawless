locals {
  name_prefix = "clawless-${var.client_slug}"
  tags = merge(var.tags, {
    Client = var.client_slug
  })
}

# ── Lightsail Instance ────────────────────────────────────────────────────────

resource "aws_lightsail_instance" "this" {
  name              = local.name_prefix
  availability_zone = var.availability_zone
  blueprint_id      = var.blueprint_id
  bundle_id         = var.bundle_id

  tags = local.tags
}

# ── Lightsail Firewall ────────────────────────────────────────────────────────
#
# aws_lightsail_instance_public_ports replaces the entire firewall rule set —
# it is not additive. Port 18789 (OpenClaw gateway) is intentionally absent.
# Nodes connect to the gateway only via the Tailscale 100.x.x.x interface.

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

# ── IAM User (Bedrock access) ─────────────────────────────────────────────────
#
# Lightsail does not support EC2-style instance profiles, so each client gets
# a dedicated IAM user whose access key is injected via the Ansible env file.

resource "aws_iam_user" "bedrock" {
  name = "${local.name_prefix}-bedrock"
  path = "/clawless/${var.client_slug}/"

  tags = merge(local.tags, {
    Purpose = "bedrock-inference"
  })
}

resource "aws_iam_access_key" "bedrock" {
  user = aws_iam_user.bedrock.name
}

resource "aws_iam_user_policy" "bedrock" {
  name = "bedrock-invoke"
  user = aws_iam_user.bedrock.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "BedrockInvokeModel"
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
        ]
        Resource = "*"
      }
    ]
  })
}
