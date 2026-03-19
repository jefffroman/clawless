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

variable "ansible_s3_bucket" {
  description = "S3 bucket name where ansible playbooks are published. Instances sync from here at boot before self-provisioning."
  type        = string
}

variable "key_pair_name" {
  description = "Lightsail key pair name for Ansible SSH access."
  type        = string
}

variable "provisioner_cidr" {
  description = "CIDR of the machine running tofu apply. Setup ports (22, 80, 443) are restricted to this IP."
  type        = string
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
