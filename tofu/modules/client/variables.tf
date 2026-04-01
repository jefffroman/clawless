variable "agent_slug" {
  description = "Short unique identifier for this agent (alphanumeric + hyphens, format: {client}-{agent}). Used in all resource names."
  type        = string
}

variable "client_name" {
  description = "Human-readable client (customer) name used in tags."
  type        = string
}

variable "availability_zone" {

  description = "Lightsail availability zone."
  type        = string
}

variable "bundle_id" {
  description = "Lightsail bundle (plan) ID."
  type        = string
}

variable "blueprint_id" {
  description = "Lightsail blueprint ID. Used only when both snapshot variables are empty."
  type        = string
}

variable "golden_snapshot_name" {
  description = "Golden snapshot name for new client provisioning. Takes precedence over blueprint_id. Empty string means use blueprint."
  type        = string
  default     = ""
}

variable "backup_bucket" {
  description = "Shared S3 backup bucket name. Agent workspace is synced to agents/{slug}/workspace/ within this bucket."
  type        = string
}

variable "clawless_version" {
  description = "Git ref (tag or branch) to clone for Ansible playbooks at boot."
  type        = string
}

variable "agent_name" {
  description = "Display name of the agent. Embedded in user-data at apply time."
  type        = string
  default     = ""
}

variable "agent_style" {
  description = "Agent style (e.g. 'assistant'). Embedded in user-data at apply time."
  type        = string
  default     = "assistant"
}

variable "agent_channel" {
  description = "Channel integration type (e.g. 'telegram'). Embedded in user-data at apply time."
  type        = string
  default     = ""
}

variable "channel_config" {
  description = "Channel-specific config map. Embedded in user-data at apply time. Null if no channel configured."
  type        = any
  default     = null
}

variable "bedrock_model" {
  description = "OpenClaw model string for this client (e.g. bedrock/us.amazon.nova-pro-v1:0)."
  type        = string
  default     = "bedrock/us.amazon.nova-lite-v1:0"
}

variable "is_new" {
  description = "True for agents being provisioned for the first time. Uses golden snapshot (or blueprint) instead of the pause snapshot. False (default) assumes a pause snapshot exists and errors if it does not."
  type        = bool
  default     = false
}

variable "active" {
  description = "When false, ephemeral resources (Lightsail instance, SSM activation, firewall) are destroyed. Durable resources (S3, IAM) are preserved so workspace data and the IAM role survive."
  type        = bool
  default     = true
}

variable "lifecycle_sfn_arn" {
  description = "ARN of the lifecycle Step Functions state machine. Baked into the self-sleep helper and used for IAM scoping."
  type        = string
}

variable "wake_listener_url" {
  description = "Function URL of the wake listener Lambda. Baked into the self-sleep helper for setWebhook."
  type        = string
}

variable "wake_messages_table_arn" {
  description = "ARN of the clawless-wake-messages DynamoDB table."
  type        = string
}

variable "wake_messages_table_name" {
  description = "Name of the clawless-wake-messages DynamoDB table. Baked into the wake-greet script."
  type        = string
}

variable "tags" {
  description = "Tags to merge onto all resources alongside the per-client Client tag."
  type        = map(string)
  default     = {}
}
