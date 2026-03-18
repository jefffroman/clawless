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

# After provisioning, call Ansible directly for each client.
# The gateway token is generated on the remote instance by Ansible and never
# leaves it — it is not passed here, not stored in state, not written locally.
resource "null_resource" "provision" {
  for_each = var.clients

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
        playbooks/provision.yml
    EOT
  }

  depends_on = [module.client]
}
