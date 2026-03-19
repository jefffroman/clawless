output "backup_bucket_names" {
  description = "S3 backup bucket names for all clients, keyed by client slug."
  value = {
    for slug, mod in module.client : slug => mod.backup_bucket_name
  }
}
