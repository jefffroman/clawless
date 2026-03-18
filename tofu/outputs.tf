output "client_public_ips" {
  description = "Public IP addresses of all provisioned Lightsail instances, keyed by client slug."
  value = {
    for slug, mod in module.client : slug => mod.instance_public_ip
  }
}
