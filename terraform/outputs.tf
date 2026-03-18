output "client_public_ips" {
  description = "Public IP addresses of all provisioned Lightsail instances, keyed by client slug."
  value = {
    for slug, mod in module.client : slug => mod.instance_public_ip
  }
}

output "client_gateway_tokens" {
  description = "OpenClaw gateway tokens, keyed by client slug. Sensitive — use tofu output -json to retrieve."
  sensitive   = true
  value = {
    for slug, mod in module.client : slug => mod.gateway_token
  }
}
