output "instance_public_ip" {
  description = "Public IP address of the Lightsail instance. Null when inactive or created from snapshot (fetch via AWS CLI if needed)."
  value       = var.active && !local.use_snapshot ? one(aws_lightsail_instance.this[*].public_ip_address) : null
}

output "instance_name" {
  description = "Lightsail instance name. Null when inactive."
  value       = var.active ? local.name_prefix : null
}

output "instance_created_trigger" {
  description = "Changes whenever the instance is replaced. Used by null_resource.provision to re-trigger Ansible on instance recreation."
  value = var.active ? (
    local.use_snapshot
    ? one(null_resource.instance_from_snapshot[*].id)
    : one(aws_lightsail_instance.this[*].id)
  ) : null
}

output "is_resume" {
  description = "True when a per-client pause snapshot was discovered. Ansible is skipped for resumes — the instance boots fully configured."
  value       = local.client_snap != ""
}

output "backup_bucket_name" {
  description = "S3 backup bucket name (primary region) for this client's workspace."
  value       = aws_s3_bucket.workspace_backup.id
}
