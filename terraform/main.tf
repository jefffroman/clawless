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
  for_each = var.clients

  providers = {
    aws        = aws
    aws.backup = aws.backup
  }

  client_slug            = each.key
  display_name           = each.value.display_name
  active                 = each.value.active
  availability_zone      = var.lightsail_availability_zone
  bundle_id              = var.lightsail_bundle_id
  blueprint_id           = var.lightsail_blueprint_id
  key_pair_name          = aws_lightsail_key_pair.ansible.name
  provisioner_cidr       = local.provisioner_cidr
  tags                   = var.tags
}

# After provisioning, call Ansible directly for each client.
# The gateway token is generated on the remote instance by Ansible and never
# leaves it — it is not passed here, not stored in state, not written locally.
resource "null_resource" "provision" {
  for_each = { for k, v in var.clients : k => v if v.active }

  triggers = {
    instance_name = module.client[each.key].instance_name
  }

  provisioner "local-exec" {
    working_dir = "${path.root}/../ansible"
    command     = <<-EOT
      ansible-playbook \
        -i "${module.client[each.key].instance_public_ip}," \
        -e "client_slug=${each.key}" \
        -e "openclaw_bedrock_region=${var.aws_region}" \
        -e "openclaw_backup_bucket=${module.client[each.key].backup_bucket_name}" \
        playbooks/provision.yml
    EOT
  }

  depends_on = [module.client]
}
