output "instance_name" {
  description = "Lightsail instance name. Null when inactive."
  value       = var.active ? local.name_prefix : null
}


