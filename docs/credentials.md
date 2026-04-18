# Credentials

See [ssm.md](ssm.md) for the full SSM Parameter Store namespace, including IAM scope per principal.

## Overview

| Credential | Where stored | Used by |
|-----------|-------------|---------|
| AWS access key + secret | `~/.aws/credentials` or env vars | All scripts, `tofu apply` |
| OpenClaw gateway token | SSM SecureString `/clawless/clients/{slug}/gateway_token` | Task def `secrets[]` → container env |
| Channel bot tokens (Telegram, etc.) | SSM SecureString at `/clawless/clients/{client}/{agent}` | Task def env via tofu |
| Bedrock credentials | Per-agent Fargate task role (automatic via ECS metadata) | OpenClaw gateway + agent tools |
| Alert email | `tofu/terraform.tfvars` | SNS → CloudWatch budget alarms |

### Sensitive files (never commit)

```
tofu/backend.hcl
tofu/terraform.tfvars
```

These are listed in `.gitignore`.

## Credential delivery to containers

Fargate tasks have a task role (`module.client.aws_iam_role.task`) that the AWS SDK discovers automatically via the ECS container metadata endpoint. No credential files, no IMDS, no SSM Hybrid Activation.

### The scrubber workaround

OpenClaw scrubs `AWS_CONTAINER_CREDENTIALS_RELATIVE_URI` from tool shells (its `host-env-security-policy.json` `blockedOverrideOnlyKeys` list). This prevents the SDK inside agent-spawned processes from finding the task role via the normal ECS metadata hint.

The gateway entrypoint works around this by writing a `credential_process` config at boot:

1. `install_aws_creds()` reads the still-intact `AWS_CONTAINER_CREDENTIALS_RELATIVE_URI` from the entrypoint's own environment
2. Creates `~/.aws/ecs-creds.sh` — curls `http://169.254.170.2${uri}` and reshapes the response into `credential_process` JSON
3. Creates `~/.aws/config` pointing at the helper script
4. The `~/.aws/` directory is excluded from S3 sync (per-boot only)

OpenClaw does not scrub `~/.aws/config`, so the SDK in tool shells picks up the credential_process and gets fresh task-role creds on every invocation.

### Task role scope

Each task role is scoped to:

- `s3://{backup_bucket}/agents/{slug}/*` (workspace sync)
- Its own ECS service (so the gateway can self-stop on sleep)
- `ssm:PutParameter` on its own `/active` parameter (for self-sleep via the sleep skill)
- `states:StartExecution` on the lifecycle SFN (to trigger pause/resume)
- Bedrock `InvokeModel` / `InvokeModelWithResponseStream`
- CloudWatch Logs (its own log group)
- DynamoDB wake messages table (message replay on boot)

## Gateway token

The OpenClaw gateway token authenticates API requests to the gateway. It is:

- Generated at agent creation time (`openssl rand -hex 32`)
- Stored as an SSM SecureString at `/clawless/clients/{slug}/gateway_token`
- Injected into the container via the task definition's `secrets[]` block
- Never written to the workspace or synced to S3

## Channel bot tokens

Channel credentials (Telegram bot tokens, Discord tokens, etc.) are stored in the agent's SSM SecureString parameter. At `tofu apply` time, they are embedded in the task definition as environment variables. The credentials are encrypted at rest in SSM and in transit via HTTPS.
