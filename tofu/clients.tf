# Agent list is sourced from SSM Parameter Store under /clawless/clients:
#
#   /clawless/clients/{client_slug}/{agent_slug}        → {"client_name": "Acme Corp", "agent_name": "Aria", ...}
#   /clawless/clients/{client_slug}/{agent_slug}/active → "true" or "false"
#
# The /active parameter is split out so agents can pause themselves via a
# tightly scoped IAM policy (ssm:PutParameter on their own /active path only).
#
# Client slug uniqueness is enforced by the storefront (clawless-platform).
# Tofu derives a globally unique agent key: "{client_slug}/{agent_slug}".

data "aws_ssm_parameters_by_path" "clawless" {
  path            = "/clawless/clients"
  recursive       = true
  with_decryption = true
}

locals {
  # Flatten into path → raw value map, stripping SSM's sensitivity marker.
  # Pre-split each path once so downstream locals can index by segment.
  _params = {
    for path, val in nonsensitive(zipmap(
      data.aws_ssm_parameters_by_path.clawless.names,
      data.aws_ssm_parameters_by_path.clawless.values
    )) :
    path => { parts = split("/", path), val = val }
  }

  # Active flags: /clawless/clients/{client_slug}/{agent_slug}/active — 6 segments
  _active = {
    for path, p in local._params :
    "${p.parts[3]}/${p.parts[4]}" => p.val == "true"
    if length(p.parts) == 6 && p.parts[5] == "active"
  }

  # Agent records with active flag overridden from the separate /active parameter.
  # /clawless/clients/{client_slug}/{agent_slug} — exactly 5 path segments
  # Key format: "{client_slug}/{agent_slug}" (slash-separated, matches SSM path)
  agents = {
    for path, p in local._params :
    "${p.parts[3]}/${p.parts[4]}" => merge(
      jsondecode(p.val),
      { active = lookup(local._active, "${p.parts[3]}/${p.parts[4]}", true) }
    )
    if length(p.parts) == 5
  }
}
