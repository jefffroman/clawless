variable "aws_region" {
  description = "AWS region for Lightsail and Bedrock resources."
  type        = string
  default     = "us-east-1"
}

variable "lightsail_availability_zone" {
  description = "Lightsail availability zone (must be in var.aws_region)."
  type        = string
  default     = "us-east-1a"
}

variable "lightsail_bundle_id" {
  description = "Lightsail bundle ID — medium_3_0 is the 4GB/2vCPU/$24/mo plan (minimum recommended for OpenClaw)."
  type        = string
  default     = "medium_3_0"
}

variable "lightsail_blueprint_id" {
  description = "Lightsail blueprint ID for OpenClaw. Verify exact ID in the AWS Lightsail console — it evolves with OpenClaw releases."
  type        = string
  default     = "openclaw"
}

variable "provisioner_public_key_path" {
  description = "Path to the public key used by Ansible for SSH provisioning."
  type        = string
  default     = "~/.ssh/clawless_ansible.pub"
}

variable "backup_region" {
  description = "Secondary AWS region for S3 cross-region replication. Should be geographically distant from aws_region."
  type        = string
  default     = "us-west-2"
}

variable "alert_email" {
  description = "Email address for operator alerts (Bedrock budget, backup failures). Requires manual confirmation of the SNS subscription after first apply."
  type        = string
}

variable "bedrock_monthly_budget_usd" {
  description = "Monthly Bedrock spend threshold in USD. Alerts fire at 80% and 100%."
  type        = number
  default     = 50
}

variable "tags" {
  description = "Tags applied to all taggable resources."
  type        = map(string)
  default = {
    Project   = "clawless"
    ManagedBy = "terraform"
  }
}
