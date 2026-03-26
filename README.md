# Clawless

**An on-demand, resumable, serverless OpenClaw platform for AWS.**

Clawless provisions isolated [OpenClaw](https://openclaw.ai) agent instances on AWS Lightsail. Each agent gets its own Lightsail instance, IAM role, Bedrock access, and workspace backed up to S3. Instances can be paused (snapshotted) when idle and resumed in minutes — paying only for storage when paused.

---

## Architecture Overview

```
SSM Parameter Store (/clawless/clients/{client}/{agent})
        |
        v
EventBridge → Lifecycle Lambda (tofu apply)
        |
        +-- per-agent Lightsail instance (from golden snapshot)
        |       |
        |       +-- user-data: ansible-playbook provision-client.yml (local)
        |       +-- hourly: aws s3 sync workspace → S3 backup bucket
        |       |
        |       +-- ubuntu user: runs OpenClaw gateway (user-level systemd)
        |       +-- agent user: owns workspace (/home/agent), runs tools in Docker sandbox
        |       +-- SearXNG: local web search (loopback, no API keys)
        |
        +-- per-agent IAM role (Bedrock, S3, CloudWatch)
        +-- per-agent SSM Hybrid Activation (temporary rotating creds)
        +-- shared S3 backup bucket (per-agent prefix, cross-region replication)
```

- **Self-provisioning**: Instances configure themselves at boot via user-data (no inbound SSH required after bake).
- **Admin access**: Via AWS SSM Session Manager — no port 22 open on production instances.
- **Pause/resume**: Snapshot → destroy instance → restore from snapshot. Workspace persists in S3.
- **Sandbox isolation**: Tool execution runs in a Docker container (`openclaw-sandbox-common:bookworm-slim`) as the `agent` user. The gateway (ubuntu user) manages the container; tools never run on the host directly.
- **Agent memory**: 3-layer system — human-editable Markdown, ChromaDB vector search, NetworkX knowledge graph — auto-reindexed every 5 minutes.
- **Web search**: Self-hosted SearXNG on each instance — no API keys, no per-query costs. Installed as a ClawHub skill (`openclaw skills install searxng`).
- **Credential delivery**: Lightsail IMDS provides the wrong IAM role, so instances use `credential_process` with a self-assume helper script. The AWS SDK auto-refreshes credentials before expiry.
- **Lifecycle automation**: All agent operations (add, remove, pause, resume) are driven by SSM parameter changes, triggering the Lifecycle Lambda via EventBridge.

---

## Terminology

- **Client**: the human customer (e.g. "Acme Corp"). One client may have multiple agents.
- **Agent**: one OpenClaw instance serving a client. Identified by `{client-slug}/{agent-slug}` (SSM path) or `{client-slug}-{agent-slug}` (AWS resource names).

---

## Prerequisites

### Tools

| Tool | Version | macOS | Ubuntu |
|------|---------|-------|--------|
| [OpenTofu](https://opentofu.org/docs/intro/install/) | >= 1.10 | `brew install opentofu` | `snap install opentofu --classic` |
| [Ansible](https://docs.ansible.com/ansible/latest/installation_guide/) | >= 2.14 | `brew install ansible` | `pip3 install ansible` |
| [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) | >= 2.x | `brew install awscli` | official Linux installer (see link) |
| [Docker](https://docs.docker.com/get-docker/) | >= 24 | [Docker Desktop](https://www.docker.com/products/docker-desktop/) | `apt install docker.io` |
| Python 3 | >= 3.10 | `brew install python` | `apt install python3` |
| jq | any | `brew install jq` | `apt install jq` |
| openssl | any | `brew install openssl` | pre-installed |

### AWS Credentials

You need an IAM user or role with the following permissions:

```
lightsail:*
s3:*
iam:*
ssm:*
sns:*
cloudwatch:*
budgets:*
ecr:*
events:*
lambda:*
bedrock:InvokeModel
ce:GetCostAndUsage        (for check-costs.py)
```

Configure credentials before running any scripts:

```bash
aws configure
# or
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_DEFAULT_REGION=us-east-1
```

Verify:

```bash
aws sts get-caller-identity
```

---

## First-Time Setup

### 1. Bootstrap

Run once to create the S3 state bucket and write local config files:

```bash
./scripts/bootstrap.sh
```

You will be prompted for:
- **AWS region** — primary region for Lightsail instances (e.g., `us-east-1`)
- **Alert email** — receives Bedrock budget alerts and backup failure notifications
- **Clawless version tag** — git tag or branch the Lifecycle Lambda will clone on each run (default: `latest`)

This creates:
- S3 bucket for OpenTofu state (versioned + encrypted)
- `tofu/backend.hcl` — backend config (gitignored)
- `tofu/terraform.tfvars` — variable values (gitignored)
- SSM Parameter `/clawless/version` — controls which git ref the Lambda clones

### 2. Initialize OpenTofu

```bash
cd tofu
tofu init -backend-config=backend.hcl
```

### 3. Bake the Golden Snapshot

The golden snapshot pre-installs slow dependencies (Python packages, ansible-core, playbooks) so that per-agent provisioning is fast. **Bake once before adding your first agent.** Re-bake whenever system packages or base Ansible playbooks change.

```bash
./scripts/bake-snapshot.sh
```

This will:
1. Generate an SSH key pair at `~/.ssh/clawless_ansible` if absent, and upload it to Lightsail
2. Spin up a temporary Lightsail instance from the base blueprint
3. Run `ansible/playbooks/provision-base.yml` via SSH (port 22 open on bake instance only)
4. Stop the instance and create a snapshot named `clawless-golden-<timestamp>`
5. Write `golden_snapshot_name` to `tofu/terraform.tfvars` and upload it to S3
6. Run `tofu apply` to register the new snapshot name in state and build/push the Lifecycle Lambda image
7. Clean up the temporary instance and SSM activation

Bake takes approximately 10–15 minutes. Port 22 is never open on production agent instances.

### 4. Add Your First Agent

```bash
./scripts/add-agent.sh
```

Prompts for:
- **Client name** — the customer's display name (e.g. `Acme Corp`); auto-slugified
- **Agent name** — the agent's name (e.g. `Aria`); auto-slugified
- **Channel** — `telegram`, `discord`, `slack`, or `other`
- **Bot token / channel credentials** — stored as a SecureString in SSM

This writes two SSM parameters:
- `/clawless/clients/{client-slug}` — client namespace record (String)
- `/clawless/clients/{client-slug}/{agent-slug}` — agent config including channel credentials (SecureString)

EventBridge detects the SSM write and triggers the Lifecycle Lambda, which runs `tofu apply` to provision the agent. Full boot-to-ready time is approximately 8–10 minutes.

Verify an instance is ready:

```bash
./scripts/ssm-run.sh --slug <client-slug>-<agent-slug> "ls -la /home/ubuntu/.openclaw/.provisioned"
```

---

## Daily Operations

### Run a command on an instance

The `--slug` argument is the hyphenated form of `{client-slug}-{agent-slug}`:

```bash
./scripts/ssm-run.sh --slug acme-corp-aria "sudo -u ubuntu XDG_RUNTIME_DIR=/run/user/\$(id -u ubuntu) systemctl --user status openclaw-gateway"
./scripts/ssm-run.sh --slug acme-corp-aria "sudo -u ubuntu XDG_RUNTIME_DIR=/run/user/\$(id -u ubuntu) journalctl --user-unit openclaw-gateway -n 50"
```

### Check costs

```bash
python3 scripts/check-costs.py 7    # Last 7 days
```

### Add an agent

```bash
./scripts/add-agent.sh
```

### Remove an agent

Deletes the SSM entry and triggers the Lambda to destroy all AWS resources for that agent. If it was the client's last agent, the client namespace is also removed.

```bash
./scripts/remove-agent.sh <client-slug> <agent-slug>
# e.g.: ./scripts/remove-agent.sh acme-corp aria
```

Use `--force` to skip the confirmation prompt.

### Pause an idle agent (cost optimization)

Pausing snapshots the instance and destroys it. You pay only for the snapshot (~$0.05/GB) instead of the running instance.

```bash
./scripts/pause-agent.sh <client-slug> <agent-slug>
```

### Resume a paused agent

```bash
./scripts/resume-agent.sh <client-slug> <agent-slug>
```

The instance is recreated from its pause snapshot. Workspace is restored from S3. No re-provisioning — the instance boots fully configured.

### Restore a destroyed agent (disaster recovery)

If an instance was accidentally deleted and the pause snapshot is gone, restore from S3 backup:

```bash
./scripts/restore-agent.sh --slug <client>/<agent>
# Point-in-time recovery (before accidental data corruption):
./scripts/restore-agent.sh --slug <client>/<agent> --before "2026-03-24T12:00:00"
```

### Re-trigger the Lifecycle Lambda

If SSM is already correct but the Lambda needs to re-run (e.g., after a code fix):

```bash
./scripts/trigger-lifecycle.sh
```

### Build the Lambda container image

After changes to `lambda/Dockerfile` or `lambda/handler.py`, rebuild manually:

```bash
./scripts/build-lambda.sh
```

This is also triggered automatically by `tofu apply` when it detects changes.

### Update Ansible playbooks on running instances

If you change playbooks and want to push to a running instance without rebaking:

```bash
./scripts/publish-ansible.sh    # Sync ansible/ → S3
./scripts/ssm-run.sh --slug <client>-<agent> \
  "aws s3 sync s3://<ansible-bucket>/ansible/ /opt/clawless/ansible/ && \
   cd /opt/clawless/ansible && \
   ansible-playbook playbooks/provision-client.yml -i localhost, -c local \
     -e agent_slug=<client>/<agent> -e client_name='Client Name' ..."
```

> Playbook changes that affect the golden snapshot (new system packages, new base roles) require a rebake.

### Deploy a tofu or Lambda code change

The Lifecycle Lambda clones the repo at the ref stored in SSM `/clawless/version` on every invocation.

- **Test a branch** (no tagging needed): `aws ssm put-parameter --name /clawless/version --value my-branch --overwrite --region us-east-1`
- **Release**: move the `latest` tag to the new commit and set `/clawless/version` back to `latest`

Lambda handler changes also require rebuilding the container image. This happens automatically when `tofu apply` is run locally (triggered by `null_resource.lambda_image` detecting changes to `handler.py` or `Dockerfile`).

Non-client infrastructure changes (Lambda, EventBridge, alerts, IAM) must be applied **locally**:

```bash
cd tofu && tofu apply
```

Client lifecycle changes (add/pause/resume/remove) are handled automatically by the Lambda — do not run `tofu apply` manually for these.

---

## Credentials Reference

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

---

## Troubleshooting

**Check if an instance is provisioned:**
```bash
./scripts/ssm-run.sh --slug <client>-<agent> "ls -la /home/ubuntu/.openclaw/.provisioned"
```

**View provision logs:**
```bash
./scripts/ssm-run.sh --slug <client>-<agent> "cat /var/log/cloud-init-output.log"
```

**Check OpenClaw service** (user-level systemd under ubuntu):
```bash
./scripts/ssm-run.sh --slug <client>-<agent> \
  "sudo -u ubuntu XDG_RUNTIME_DIR=/run/user/\$(id -u ubuntu) systemctl --user status openclaw-gateway"
./scripts/ssm-run.sh --slug <client>-<agent> \
  "sudo -u ubuntu XDG_RUNTIME_DIR=/run/user/\$(id -u ubuntu) journalctl --user-unit openclaw-gateway -n 50"
```

**Check backup status:**
```bash
./scripts/ssm-run.sh --slug <client>-<agent> "systemctl status clawless-backup.timer"
```

**Check SearXNG:**
```bash
./scripts/ssm-run.sh --slug <client>-<agent> "systemctl status searxng && curl -s http://127.0.0.1:8080/healthz"
```

---

## Contributing

Feature requests, bug reports, and patches are welcome. Open an issue or pull request on GitHub.
