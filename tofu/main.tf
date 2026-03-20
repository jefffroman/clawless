module "client" {
  source   = "./modules/client"
  for_each = local.clients

  client_slug          = each.key
  display_name         = each.value.display_name
  active               = try(each.value.active, true)
  agent_name           = try(each.value.agent_name, "")
  agent_style          = try(each.value.agent_style, "assistant")
  agent_channel        = try(each.value.agent_channel, "")
  channel_config       = try(each.value.channel_config, null)
  availability_zone    = var.lightsail_availability_zone
  bundle_id            = var.lightsail_bundle_id
  blueprint_id         = var.lightsail_blueprint_id
  golden_snapshot_name = var.golden_snapshot_name
  backup_bucket        = aws_s3_bucket.backups.id
  ansible_s3_bucket    = var.ansible_s3_bucket
  tags                 = var.tags
}
