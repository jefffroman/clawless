# Auto-detect the public IP of the machine running tofu apply.
# Used to restrict setup ports (22, 80, 443) to the provisioner only.
# NOTE: port 443 may need opening to 0.0.0.0/0 if webhook-based channel
# integrations are used (Telegram/WhatsApp/Slack webhooks require inbound 443).
data "http" "provisioner_ip" {
  url = "https://checkip.amazonaws.com"
}

locals {
  provisioner_cidr = "${chomp(data.http.provisioner_ip.response_body)}/32"
}

module "client" {
  source   = "./modules/client"
  for_each = local.clients

  providers = {
    aws        = aws
    aws.backup = aws.backup
  }

  client_slug          = each.key
  display_name         = each.value.display_name
  active               = try(each.value.active, true)
  availability_zone    = var.lightsail_availability_zone
  bundle_id            = var.lightsail_bundle_id
  blueprint_id         = var.lightsail_blueprint_id
  golden_snapshot_name = var.golden_snapshot_name
  ansible_s3_bucket    = var.ansible_s3_bucket
  key_pair_name        = aws_lightsail_key_pair.ansible.name
  provisioner_cidr     = local.provisioner_cidr
  tags                 = var.tags
}
