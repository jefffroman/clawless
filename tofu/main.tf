module "client" {
  source   = "./modules/client"
  for_each = local.agents

  agent_slug         = each.key
  agent_name         = try(each.value.agent_name, "")
  agent_channel      = try(each.value.agent_channel, "")
  channel_config     = try(each.value.channel_config, null)
  bedrock_model      = try(each.value.bedrock_model, "bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0")
  active             = try(each.value.active, true)
  image_uri          = "${aws_ecr_repository.gateway.repository_url}:latest"
  cluster_arn        = aws_ecs_cluster.clawless.arn
  cluster_name       = aws_ecs_cluster.clawless.name
  execution_role_arn = aws_iam_role.fargate_execution.arn
  backup_bucket      = aws_s3_bucket.backups.id
  aws_region         = var.aws_region
  subnet_ids         = [for s in aws_subnet.public : s.id]
  security_group_ids = [aws_security_group.fargate_tasks.id]
  tags               = var.tags
}
