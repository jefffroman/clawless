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
  default     = "openclaw_ls_1_0"
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

variable "ansible_s3_bucket" {
  description = "S3 bucket name where the ansible directory is published by publish-ansible.sh. Instances sync playbooks from here at boot. Typically the tofu state bucket."
  type        = string
}

variable "golden_snapshot_name" {
  description = "Lightsail snapshot name to use for new client instances instead of the blueprint. Set by bake-snapshot.sh after a successful golden bake. Empty string means fall back to blueprint_id."
  type        = string
  default     = ""
}

variable "tags" {
  description = "Tags applied to all taggable resources."
  type        = map(string)
  default = {
    Project   = "clawless"
    ManagedBy = "terraform"
  }
}
