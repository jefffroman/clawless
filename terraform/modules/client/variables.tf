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
  description = "Lightsail blueprint ID."
  type        = string
}

variable "provisioner_cidr" {
  description = "CIDR of the machine running tofu apply. Setup ports (22, 80, 443) are restricted to this IP."
  type        = string
}

variable "openclaw_workspace_dir" {
  description = "Path on the instance containing OpenClaw's agent workspace."
  type        = string
}

variable "tags" {
  description = "Tags to merge onto all resources alongside the per-client Client tag."
  type        = map(string)
  default     = {}
}
