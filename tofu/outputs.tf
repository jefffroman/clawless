output "backup_bucket" {
  description = "Shared S3 backup bucket name. Client workspaces are stored under clients/{slug}/workspace/."
  value       = aws_s3_bucket.backups.id
}

output "sns_topic_arn" {
  description = "ARN of the clawless-alerts SNS topic. Used by clawless-platform to route signup, payment, and operational alerts to the same topic."
  value       = aws_sns_topic.alerts.arn
}

output "state_bucket" {
  description = "Name of the S3 bucket used for OpenTofu state. Used by clawless-platform as its backend bucket and for any cross-repo remote_state references."
  value       = local.state_bucket
}

output "lifecycle_lambda_arn" {
  description = "ARN of the lifecycle Lambda. clawless-platform can invoke this directly to trigger client provisioning outside of the SSM → EventBridge path."
  value       = aws_lambda_function.lifecycle.arn
}

output "archive_bucket" {
  description = "Name of the shared S3 archive bucket where removed clients' data is retained for 90 days."
  value       = aws_s3_bucket.backups.bucket
}
