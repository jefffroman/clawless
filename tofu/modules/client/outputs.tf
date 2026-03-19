output "instance_name" {
  description = "Lightsail instance name. Null when inactive."
  value       = var.active ? local.name_prefix : null
}


output "backup_bucket_name" {
  description = "S3 backup bucket name (primary region) for this client's workspace."
  value       = aws_s3_bucket.workspace_backup.id
}
