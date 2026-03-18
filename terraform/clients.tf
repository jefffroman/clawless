# Client list is sourced from SSM Parameter Store rather than tfvars.
# The storefront Lambda writes to this parameter on signup/cancellation.
# For manual provisioning or testing, update the parameter directly:
#
#   aws ssm put-parameter \
#     --name "/clawless/clients" \
#     --type String \
#     --overwrite \
#     --value '{"acme":{"display_name":"Acme Corp","active":true}}'
#
# The parameter must exist before running tofu plan. The bootstrap-state.sh
# script creates it empty ({}) on first run.

data "aws_ssm_parameter" "clients" {
  name = "/clawless/clients"
}

locals {
  clients = jsondecode(data.aws_ssm_parameter.clients.value)
}
