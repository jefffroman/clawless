output "client_public_ips" {
  description = "Public IP addresses of all provisioned Lightsail instances, keyed by client slug."
  value = {
    for slug, mod in module.client : slug => mod.instance_public_ip
  }
}

output "client_iam_access_keys" {
  description = "IAM access key IDs for Bedrock, keyed by client slug (non-sensitive)."
  value = {
    for slug, mod in module.client : slug => mod.iam_access_key_id
  }
}

output "client_iam_secret_keys" {
  description = "IAM secret access keys for Bedrock, keyed by client slug. Use terraform output -json to retrieve."
  sensitive   = true
  value = {
    for slug, mod in module.client : slug => mod.iam_secret_access_key
  }
}

output "ansible_inventory_snippet" {
  description = "Paste into ansible/inventory/hosts.yml under clawless_nodes.hosts after provisioning."
  value = join("\n", [
    for slug, mod in module.client :
    "        ${slug}:\n          ansible_host: ${mod.instance_public_ip}"
  ])
}
