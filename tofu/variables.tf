variable "aws_region" {
  description = "AWS region for Fargate and Bedrock resources."
  type        = string
  default     = "us-east-1"
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

variable "replica_region" {
  description = "AWS region for the S3 backup replica bucket. Should be geographically separate from aws_region."
  type        = string
  default     = "us-east-2"
}

variable "tags" {
  description = "Tags applied to all taggable resources."
  type        = map(string)
  default = {
    Project   = "clawless"
    ManagedBy = "terraform"
  }
}
