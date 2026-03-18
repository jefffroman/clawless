module "client" {
  source   = "./modules/client"
  for_each = var.clients

  client_slug       = each.key
  display_name      = each.value.display_name
  availability_zone = var.lightsail_availability_zone
  bundle_id         = var.lightsail_bundle_id
  blueprint_id      = var.lightsail_blueprint_id
  tags              = var.tags
}
