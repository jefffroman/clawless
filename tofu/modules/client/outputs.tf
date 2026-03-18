output "instance_public_ip" {
  description = "Public IP address of the Lightsail instance. Null when inactive."
  value       = one(aws_lightsail_instance.this[*].public_ip_address)
}

output "instance_name" {
  description = "Lightsail instance name. Null when inactive."
  value       = one(aws_lightsail_instance.this[*].name)
}

output "backup_bucket_name" {
  description = "S3 backup bucket name (primary region) for this client's workspace."
  value       = aws_s3_bucket.workspace_backup.id
}
