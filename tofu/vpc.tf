# ── Clawless Fargate VPC ──────────────────────────────────────────────────────
# Minimal public-subnet VPC for Fargate gateway tasks. Dev posture: tasks run
# with public IPs and reach Bedrock / Telegram / ECR / CloudWatch directly —
# no NAT gateway, no VPC endpoints. Phase 7 flips these to private subnets +
# endpoints when clawless-platform is ready to go to market.

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  fargate_azs = slice(data.aws_availability_zones.available.names, 0, 2)
}

resource "aws_vpc" "clawless" {
  cidr_block           = "10.42.0.0/16"
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = merge(var.tags, { Name = "clawless" })
}

resource "aws_internet_gateway" "clawless" {
  vpc_id = aws_vpc.clawless.id
  tags   = merge(var.tags, { Name = "clawless" })
}

resource "aws_subnet" "public" {
  for_each = { for idx, az in local.fargate_azs : az => idx }

  vpc_id                  = aws_vpc.clawless.id
  availability_zone       = each.key
  cidr_block              = cidrsubnet(aws_vpc.clawless.cidr_block, 8, each.value)
  map_public_ip_on_launch = true

  tags = merge(var.tags, { Name = "clawless-public-${each.key}" })
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.clawless.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.clawless.id
  }

  tags = merge(var.tags, { Name = "clawless-public" })
}

resource "aws_route_table_association" "public" {
  for_each       = aws_subnet.public
  subnet_id      = each.value.id
  route_table_id = aws_route_table.public.id
}

# Shared task security group. Default-deny ingress (gateway binds loopback
# inside the task), unrestricted egress for Bedrock/Telegram/ECR/Logs.
resource "aws_security_group" "fargate_tasks" {
  name        = "clawless-fargate-tasks"
  description = "Clawless Fargate gateway tasks — egress only"
  vpc_id      = aws_vpc.clawless.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, { Name = "clawless-fargate-tasks" })
}
