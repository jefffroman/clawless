# ── SSM Credential Refresh ────────────────────────────────────────────────────
# Lightsail instances use IMDS to get the Lightsail-native IAM role, which is
# in AWS's managed account and has no access to our Bedrock or S3 resources.
# The SSM agent, however, runs with the per-client managed instance role
# (clawless-{slug}-ssm). We exploit this by running a credential refresh
# document via SSM State Manager every 45 minutes; each run calls
# sts:AssumeRole on the managed instance role to obtain a fresh 1-hour session
# and writes it to /home/ubuntu/.aws/credentials as [default].
#
# apply_only_at_cron_interval defaults to false, so the document also runs
# immediately when a new instance first registers — bootstrapping credentials
# before the instance needs them.

resource "aws_ssm_document" "credential_refresh" {
  name          = "Clawless-CredentialRefresh"
  document_type = "Command"

  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Refresh OpenClaw AWS credentials from managed instance role"
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "RefreshCredentials"
      inputs = {
        runCommand = compact(split("\n", file("${path.module}/files/credential-refresh.sh")))
      }
    }]
  })

  tags = var.tags
}

resource "aws_ssm_association" "credential_refresh" {
  name                = aws_ssm_document.credential_refresh.name
  schedule_expression = "rate(45 minutes)"
  # apply_only_at_cron_interval defaults to false: runs immediately when a new
  # instance matches the target, then on the schedule thereafter.

  targets {
    key    = "tag:Project"
    values = [var.tags["Project"]]
  }
}
