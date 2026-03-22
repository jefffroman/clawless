# Agent list is sourced from SSM Parameter Store, organized as a two-level
# hierarchy under /clawless/clients:
#
#   /clawless/clients/{client_slug}               → {"client_name": "Acme Corp"}
#   /clawless/clients/{client_slug}/{agent_slug}  → {"agent_name": "Aria", "active": true, ...}
#
# Client records are created atomically (--no-overwrite) by add-agent.sh,
# guaranteeing client slug uniqueness. Agent slugs are unique within a client.
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

  # Client records: /clawless/clients/{client_slug} — exactly 4 path segments
  _clients = {
    for path, val in local._params :
    element(split("/", path), 3) => jsondecode(val)
    if length(split("/", path)) == 4
  }

  # Agent records joined with client_name:
  # /clawless/clients/{client_slug}/{agent_slug} — exactly 5 path segments
  # Key format: "{client_slug}/{agent_slug}" (slash-separated, matches SSM path)
  agents = {
    for path, val in local._params :
    "${element(split("/", path), 3)}/${element(split("/", path), 4)}" => merge(
      jsondecode(val),
      { client_name = local._clients[element(split("/", path), 3)].client_name }
    )
    if length(split("/", path)) == 5
  }
}
