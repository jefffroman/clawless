# Credentials

See [ssm.md](ssm.md) for the full SSM Parameter Store namespace, including IAM scope per principal.

## Overview

| Credential | Where stored | Used by |
|-----------|-------------|---------|
| AWS access key + secret | `~/.aws/credentials` or env vars | All scripts, `tofu apply` |
| Channel bot tokens (Telegram, etc.) | SSM SecureString at `/clawless/clients/{client}/{agent}` | Task def env via tofu |
| Bedrock credentials | Per-agent Fargate task role (automatic via ECS metadata) | clawless-gateway + agent tools |
| Alert email | `tofu/terraform.tfvars` | SNS → CloudWatch budget alarms |

### Sensitive files (never commit)

```
tofu/backend.hcl
tofu/terraform.tfvars
```

These are listed in `.gitignore`.

## Credential delivery to containers

Fargate tasks have a task role (`module.client.aws_iam_role.task`) that the AWS SDK discovers automatically via the ECS container metadata endpoint. boto3 inside `clawless-gateway` reads `AWS_CONTAINER_CREDENTIALS_RELATIVE_URI` natively — no `credential_process` shim, no IMDS, no SSM Hybrid Activation.

The `bash` tool inherits the container's env (with a small whitelist), so any subshell the agent spawns also picks up task-role creds via the same metadata hint.

### Task role scope

Each task role is scoped to:

- `s3://{backup_bucket}/agents/{slug}/*` (workspace sync)
- Its own ECS service (so the gateway can self-stop on sleep)
- `ssm:PutParameter` on its own `/active` parameter (for self-sleep via the sleep tool)
- `states:StartExecution` on the lifecycle SFN (to trigger pause/resume)
- Bedrock `InvokeModel` / `InvokeModelWithResponseStream` / `Converse` / `ConverseStream`
- CloudWatch Logs (its own log group)
- DynamoDB wake messages table (claim-deliver-delete on boot)

## Channel bot tokens

Channel credentials (Telegram bot tokens, Discord tokens, etc.) are stored in the agent's SSM SecureString parameter. At `tofu apply` time, they are embedded in the task definition as environment variables. The credentials are encrypted at rest in SSM and in transit via HTTPS.
