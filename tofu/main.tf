module "client" {
  source   = "./modules/client"
  for_each = local.lightsail_agents

  agent_slug               = each.key
  client_name              = each.value.client_name
  is_new                   = contains(var.new_agent_slugs, each.key)
  bedrock_model            = try(each.value.bedrock_model, "bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0")
  active                   = try(each.value.active, true)
  agent_name               = try(each.value.agent_name, "")
  agent_style              = try(each.value.agent_style, "assistant")
  agent_channel            = try(each.value.agent_channel, "")
  channel_config           = try(each.value.channel_config, null)
  availability_zone        = var.lightsail_availability_zone
  bundle_id                = var.lightsail_bundle_id
  blueprint_id             = var.lightsail_blueprint_id
  golden_snapshot_name     = var.golden_snapshot_name
  backup_bucket            = aws_s3_bucket.backups.id
  clawless_version         = data.aws_ssm_parameter.version.value
  lifecycle_sfn_arn        = aws_sfn_state_machine.lifecycle.arn
  wake_messages_table_arn  = aws_dynamodb_table.wake_messages.arn
  wake_messages_table_name = aws_dynamodb_table.wake_messages.name
  tags                     = var.tags
}

module "client_fargate" {
  source   = "./modules/client-fargate"
  for_each = local.fargate_agents

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
