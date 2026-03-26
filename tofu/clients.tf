# Agent list is sourced from SSM Parameter Store under /clawless/clients:
#
#   /clawless/clients/{client_slug}/{agent_slug} → {"client_name": "Acme Corp", "agent_name": "Aria", "active": true, ...}
#
# Client slug uniqueness is enforced by the storefront (clawless-platform).
# Tofu derives a globally unique agent key: "{client_slug}/{agent_slug}".

data "aws_ssm_parameters_by_path" "clawless" {
  path            = "/clawless/clients"
  recursive       = true
  with_decryption = true
}

locals {
  # Flatten into path → raw value map, stripping SSM's sensitivity marker
  _params = nonsensitive(zipmap(
    data.aws_ssm_parameters_by_path.clawless.names,
    data.aws_ssm_parameters_by_path.clawless.values
  ))

  # Agent records: /clawless/clients/{client_slug}/{agent_slug} — exactly 5 path segments
  # Key format: "{client_slug}/{agent_slug}" (slash-separated, matches SSM path)
  agents = {
    for path, val in local._params :
    "${element(split("/", path), 3)}/${element(split("/", path), 4)}" => jsondecode(val)
    if length(split("/", path)) == 5
  }
}
