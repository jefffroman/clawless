output "instance_public_ip" {
  description = "Public IP address of the Lightsail instance."
  value       = aws_lightsail_instance.this.public_ip_address
}

output "instance_name" {
  description = "Lightsail instance name."
  value       = aws_lightsail_instance.this.name
}

output "iam_user_name" {
  description = "IAM user name for Bedrock access."
  value       = aws_iam_user.bedrock.name
}

output "iam_access_key_id" {
  description = "IAM access key ID (non-secret)."
  value       = aws_iam_access_key.bedrock.id
}

output "iam_secret_access_key" {
  description = "IAM secret access key. Sensitive — use terraform output -json to retrieve."
  sensitive   = true
  value       = aws_iam_access_key.bedrock.secret
}
