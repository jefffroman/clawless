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
  key_pair_name        = aws_lightsail_key_pair.ansible.name
  provisioner_cidr     = local.provisioner_cidr
  tags                 = var.tags
}

# Run Ansible after provisioning for new instances (blueprint or golden snapshot path).
# Skipped for resume: the per-client snapshot was discovered by the module, and the
# instance boots fully configured with no Ansible needed.
# IP is fetched via CLI so this works for both creation paths.
resource "null_resource" "provision" {
  for_each = {
    for k, v in local.clients : k => v
    if try(v.active, true) && !module.client[k].is_resume
  }

  triggers = {
    instance_created = module.client[each.key].instance_created_trigger
  }

  provisioner "local-exec" {
    working_dir = "${path.root}/../ansible"
    # channel_config is written to a temp file so its contents (bot tokens etc.)
    # never appear on the command line or in process listings.
    command = <<-EOT
      set -e
      _ip=$(aws lightsail get-instance \
        --instance-name "clawless-${each.key}" \
        --query 'instance.publicIpAddress' \
        --output text)
      _tmpvars=$(mktemp /tmp/clawless-ansible-XXXXXX.json)
      trap 'rm -f "$_tmpvars"' EXIT
      printf '%s\n' '${jsonencode({channel_config: try(each.value.channel_config, null)})}' > "$_tmpvars"
      ansible-playbook \
        -i "$_ip," \
        -e "client_slug=${each.key}" \
        -e "display_name=${each.value.display_name}" \
        -e "openclaw_bedrock_region=${var.aws_region}" \
        -e "openclaw_backup_bucket=${module.client[each.key].backup_bucket_name}" \
        ${try(each.value.agent_name, null) != null ? "-e agent_name=${each.value.agent_name}" : ""} \
        ${try(each.value.agent_style, null) != null ? "-e agent_style=${each.value.agent_style}" : ""} \
        ${try(each.value.agent_channel, null) != null ? "-e agent_channel=${each.value.agent_channel}" : ""} \
        -e "@$_tmpvars" \
        playbooks/provision-client.yml
    EOT
  }

  depends_on = [module.client]
}
