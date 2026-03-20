variable "client_slug" {
  description = "Short unique identifier for this client (alphanumeric + hyphens). Used in all resource names."
  type        = string
}

variable "display_name" {
  description = "Human-readable client name used in IAM tags."
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
  description = "Shared S3 backup bucket name. Client workspace is synced to clients/{slug}/workspace/ within this bucket."
  type        = string
}

variable "ansible_s3_bucket" {
  description = "S3 bucket name where ansible playbooks are published. Used for IAM policy (SSM RunCommand updates to running instances)."
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

variable "active" {
  description = "When false, ephemeral resources (Lightsail instance, SSM activation, firewall) are destroyed. Durable resources (S3, IAM) are preserved so workspace data and the IAM role survive."
  type        = bool
  default     = true
}

variable "tags" {
  description = "Tags to merge onto all resources alongside the per-client Client tag."
  type        = map(string)
  default     = {}
}
