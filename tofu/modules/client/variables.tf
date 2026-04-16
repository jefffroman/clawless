variable "agent_slug" {
  description = "Agent key, format 'client_slug/agent_slug'."
  type        = string
}

variable "agent_name" {
  type    = string
  default = ""
}

variable "client_name" {
  type    = string
  default = ""
}

variable "agent_style" {
  type    = string
  default = ""
}

variable "agent_channel" {
  type    = string
  default = ""
}

variable "lifecycle_sfn_arn" {
  description = "ARN of the lifecycle SFN. The task invokes this to trigger sleep via the standard lifecycle flow."
  type        = string
}

variable "wake_listener_url" {
  description = "Function URL of the wake listener Lambda. Injected into the task env for setWebhook on sleep."
  type        = string
  default     = ""
}

variable "wake_messages_table_name" {
  type    = string
  default = ""
}

variable "wake_messages_table_arn" {
  type    = string
  default = ""
}

variable "searxng_url" {
  description = "Shared SearXNG Lambda Function URL, injected into the task env so the searxng skill can reach the search backend."
  type        = string
  default     = ""
}

variable "channel_config" {
  type    = any
  default = null
}

variable "bedrock_model" {
  type    = string
  default = "bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0"
}

variable "active" {
  description = "When false the service stays at desired_count=0."
  type        = bool
  default     = true
}

variable "image_uri" {
  description = "Gateway container image URI (e.g. ECR repo:tag)."
  type        = string
}

variable "cluster_arn" {
  type = string
}

variable "cluster_name" {
  type = string
}

variable "execution_role_arn" {
  type = string
}

variable "backup_bucket" {
  type = string
}

variable "aws_region" {
  type = string
}

# Network — defaults suit the dev public-subnet posture. Phase 7 flips these
# to private subnets + VPC endpoints via a variable swap, no module rewrite.
variable "subnet_ids" {
  type = list(string)
}

variable "security_group_ids" {
  type = list(string)
}

variable "assign_public_ip" {
  type    = bool
  default = true
}

variable "task_cpu" {
  description = "Fargate task CPU units. 2048 = 2 vCPU."
  type        = number
  default     = 2048
}

variable "task_memory" {
  description = "Fargate task memory (MB). 4096 = 4 GB."
  type        = number
  default     = 4096
}

variable "tags" {
  type    = map(string)
  default = {}
}
