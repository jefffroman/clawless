# Credentials

## Overview

| Credential | Where stored | Used by |
|-----------|-------------|---------|
| AWS access key + secret | `~/.aws/credentials` or env vars | All scripts, `tofu apply` |
| SSH key pair (`~/.ssh/clawless_ansible`) | Local filesystem + Lightsail | `bake-snapshot.sh` only — auto-generated on first bake |
| OpenClaw gateway token | Instance env file (`/etc/openclaw/openclaw.env`) | OpenClaw service — generated on first boot, never leaves instance |
| Channel bot tokens (Telegram, etc.) | SSM SecureString at `/clawless/clients/{client}/{agent}` | Embedded in user-data at `tofu apply` time |
| Bedrock credentials | Per-agent IAM role via SSM Hybrid Activation + `credential_process` | OpenClaw service (temporary, rotating) |
| Alert email | `tofu/terraform.tfvars` | SNS → CloudWatch budget alarms |

### Sensitive files (never commit)

```
tofu/backend.hcl
tofu/terraform.tfvars
~/.ssh/clawless_ansible
```

These are listed in `.gitignore`.

## Credential delivery to instances

Lightsail instances don't support IAM instance profiles. The instance metadata service (IMDS) returns Lightsail's own internal IAM role — not ours. This means the standard AWS SDK credential chain doesn't work.

### The workaround

Each instance uses `credential_process` in `~/.aws/config` to call a helper script that self-assumes the agent's IAM role:

```
[default]
credential_process = sudo /usr/local/sbin/clawless-creds-helper
```

The helper (`/usr/local/sbin/clawless-creds-helper`) does the following:

1. Uses the SSM Hybrid Activation credentials (written at instance registration) to call `sts:AssumeRole` on the agent's own IAM role (`clawless-{slug}-ssm`)
2. Returns credential_process JSON with `AccessKeyId`, `SecretAccessKey`, `SessionToken`, and `Expiration`
3. The AWS SDK automatically calls the helper again before the credentials expire — no service restarts needed

### Key details

- `AWS_EC2_METADATA_DISABLED=true` is set in `/etc/openclaw/openclaw.env` to prevent the SDK from trying IMDS first (which would return the wrong role)
- IAM role `max_session_duration = 43200` (12 hours), set in the tofu client module
- The helper is root-owned; the ubuntu user calls it via passwordless sudo
- SSM Hybrid Activation provides the bootstrap credentials that make the first `AssumeRole` call possible
- Activation tags (`Project=clawless`, `Client={slug}`) propagate to managed instances at registration for SSM targeting

### Why not role chaining?

Role chaining (assuming a role from an assumed role) has a hard 1-hour session limit imposed by AWS. The self-assume pattern avoids this — the role assumes itself, which counts as a direct assumption with the full 12-hour max.

## Gateway token

The OpenClaw gateway token authenticates API requests to the gateway. It is:

- Generated on the instance at first provision (`openssl rand -hex 32`)
- Written to `/etc/openclaw/openclaw.env` as `OPENCLAW_GATEWAY_TOKEN=...`
- Never transmitted off the instance
- Preserved across pause/resume (lives in the snapshot)

## Channel bot tokens

Channel credentials (Telegram bot tokens, Discord tokens, etc.) are stored in the agent's SSM SecureString parameter. At `tofu apply` time, they are embedded in the instance's user-data script as a base64-encoded JSON blob. The instance decodes this at boot and Ansible writes the credentials into the OpenClaw config.

The credentials are encrypted at rest in SSM and in transit via HTTPS. They appear in the user-data script in plaintext (base64-encoded), which is visible to anyone with Lightsail console access to that instance.
