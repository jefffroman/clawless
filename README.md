# Clawless

**An on-demand, resumable, serverless Openclaw platform for AWS.**

Clawless provisions isolated [OpenClaw](https://openclaw.ai) agent instances on AWS Lightsail. Each client gets their own instance, S3 workspace backup, IAM role, and Bedrock access. Instances can be paused (snapshotted) when idle and resumed in minutes — paying only for storage when paused.

---

## Architecture Overview

```
SSM Parameter Store (/clawless/clients)
        |
        v
OpenTofu (tofu apply)
        |
        +-- per-client Lightsail instance (from golden snapshot)
        |       |
        |       +-- user-data: ansible-playbook provision-client.yml (local)
        |       +-- hourly: aws s3 sync workspace → S3 backup bucket
        |
        +-- per-client S3 bucket (workspace backup, cross-region replication)
        +-- per-client IAM role (Bedrock, S3, CloudWatch)
        +-- per-client SSM Hybrid Activation (temporary rotating creds)
```

- **Self-provisioning**: Instances configure themselves at boot via user-data (no inbound SSH required after bake).
- **Admin access**: Via AWS SSM Session Manager — no port 22 open.
- **Pause/resume**: Snapshot → destroy instance → restore from snapshot. Workspace persists in S3.
- **Agent memory**: 3-layer system — human-editable Markdown, ChromaDB vector search, NetworkX knowledge graph — auto-reindexed every 5 minutes.

---

## Prerequisites

### Tools

| Tool | Version | Notes |
|------|---------|-------|
| [OpenTofu](https://opentofu.org/docs/intro/install/) | >= 1.10 | `brew install opentofu` |
| [Ansible](https://docs.ansible.com/ansible/latest/installation_guide/) | >= 2.14 | `brew install ansible` |
| [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) | >= 2.x | `brew install awscli` |
| Python 3 | >= 3.10 | for `scripts/check-costs.py` |
| jq | any | `brew install jq` |
| openssl | any | pre-installed on macOS |

### AWS Credentials

You need an AWS IAM user or role with the following permissions. The easiest approach is an IAM user with `AdministratorAccess` scoped to the resources Clawless creates, or full admin for initial setup.

**Minimum required permissions:**

```
lightsail:*
s3:*
iam:*
ssm:*
sns:*
cloudwatch:*
budgets:*
bedrock:InvokeModel
ce:GetCostAndUsage        (for check-costs.py)
```

Configure your credentials before running any scripts:

```bash
aws configure
# or
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_DEFAULT_REGION=us-east-1
```

Verify access:

```bash
aws sts get-caller-identity
```

### SSH Key Pair

Clawless uses a dedicated SSH key pair for Ansible to reach instances during the golden snapshot bake. After baking, SSH is no longer needed for day-to-day operations.

Generate a dedicated key pair (do not reuse an existing key):

```bash
ssh-keygen -t ed25519 -f ~/.ssh/clawless_ansible -C "clawless-ansible" -N ""
```

The **public key** (`~/.ssh/clawless_ansible.pub`) is uploaded to Lightsail by `bootstrap.sh`.
The **private key** (`~/.ssh/clawless_ansible`) is referenced in `ansible/ansible.cfg` and used only during `bake-snapshot.sh`.

> **Port 22 is not open on production instances.** The bake script temporarily opens it on the bake instance only, then the golden snapshot has it closed.

---

## First-Time Setup

### 1. Bootstrap

Run once to create the S3 state bucket, write config files, and register your first agent:

```bash
./scripts/bootstrap.sh
```

You will be prompted for:
- **AWS region** — primary region for Lightsail instances (e.g., `us-east-1`)
- **SSH public key** — paste the contents of `~/.ssh/clawless_ansible.pub`
- **Alert email** — receives Bedrock budget alerts and backup failure notifications

This creates:
- S3 bucket for OpenTofu state (with versioning + encryption)
- `tofu/backend.hcl` — backend config (not committed)
- `tofu/terraform.tfvars` — variable values (not committed)
- SSM Parameter `/clawless/clients` — empty client registry
- Calls `add-agent.sh` to add your first client

### 2. Add Agents (if not already done)

To add additional agents after bootstrap:

```bash
./scripts/add-agent.sh
```

Prompts for: display name, agent style, agent name, channel integration (Telegram, Discord, Slack, or none), and channel-specific credentials (bot tokens, etc.).

Client config is stored in SSM Parameter Store at `/clawless/clients` — this is the source of truth for `tofu apply`.

### 3. Bake the Golden Snapshot

The golden snapshot pre-installs slow dependencies (Python packages, ansible-core, playbooks) so that per-client provisioning is fast.

**You must bake once before your first `tofu apply`.** Re-bake whenever you want to update system packages or the base Ansible playbooks.

```bash
./scripts/bake-snapshot.sh
```

This will:
1. Spin up a temporary Lightsail instance from the base blueprint
2. Run `ansible/playbooks/provision-base.yml` via SSH (opens port 22 temporarily)
3. Stop the instance and create a snapshot named `clawless-golden-<timestamp>`
4. Write `golden_snapshot_name` to `tofu/terraform.tfvars`
5. Clean up the temporary instance and SSM activation

Bake takes approximately 10–15 minutes.

### 4. Initialize OpenTofu

```bash
cd tofu
tofu init -backend-config=backend.hcl
```

### 5. Apply

```bash
cd tofu
tofu plan
tofu apply
```

On first apply, Tofu reads clients from SSM, creates per-client resources (S3, IAM, SSM activation, Lightsail instance), and each instance self-provisions via user-data. Full boot-to-ready time is approximately 8–10 minutes per instance.

Verify an instance is ready by checking for the sentinel file:

```bash
./scripts/ssm-run.sh --slug <client-slug> "cat /var/lib/openclaw/.provisioned"
```

---

## Daily Operations

### Run a command on an instance

```bash
./scripts/ssm-run.sh --slug <client-slug> "systemctl status openclaw"
./scripts/ssm-run.sh --slug <client-slug> "tail -f /var/log/openclaw/openclaw.log"
```

### Check costs

```bash
python3 scripts/check-costs.py 7    # Last 7 days
```

### Pause an idle client (cost optimization)

Pausing snapshots the instance and destroys it. You pay only for the snapshot (~$0.05/GB) instead of the running instance (~$24/month for nano_2_0).

```bash
./scripts/pause.sh <client-slug>
```

### Resume a paused client

```bash
./scripts/resume.sh <client-slug>
```

The instance is recreated from its pause snapshot. The workspace is restored from S3. No re-provisioning — the instance boots fully configured.

### Update Ansible playbooks on running instances

If you change playbooks and want to push to a running instance without rebaking:

```bash
./scripts/publish-ansible.sh    # Sync ansible/ → S3
./scripts/ssm-run.sh --slug <client-slug> \
  "aws s3 sync s3://<ansible-bucket>/ansible/ /opt/clawless/ansible/ && \
   cd /opt/clawless/ansible && \
   ansible-playbook playbooks/provision-client.yml -i localhost, -c local"
```

> Playbook changes that affect the golden snapshot (new system packages, new base roles) require a rebake.

---

## Credentials Reference

| Credential | Where stored | Used by |
|-----------|-------------|---------|
| AWS access key + secret | `~/.aws/credentials` or env vars | All scripts, `tofu apply` |
| SSH private key (`~/.ssh/clawless_ansible`) | Local filesystem | `bake-snapshot.sh` (Ansible SSH) |
| SSH public key | Lightsail key pair (`clawless-ansible`) | Lightsail instance auth during bake |
| OpenClaw gateway token | Instance env file (`/etc/openclaw/env`) | OpenClaw service — generated on first boot, never leaves instance |
| Channel bot tokens (Telegram, etc.) | SSM `/clawless/clients`, embedded in user-data | `ansible/roles/memory/tasks/main.yml` patch |
| Bedrock credentials | Per-client IAM role via SSM Hybrid Activation | OpenClaw service (temporary, rotating) |
| Alert email | `tofu/terraform.tfvars` | SNS → CloudWatch budget alarms |

### Sensitive files (never commit)

```
tofu/backend.hcl
tofu/terraform.tfvars
~/.ssh/clawless_ansible
~/.ssh/clawless_ansible.pub
```

These are listed in `.gitignore`.

---

## Repository Layout

```
clawless/
├── ansible/
│   ├── ansible.cfg                      # SSH key, roles path, remote user
│   ├── inventory/hosts.yml.example      # Template for manual re-runs
│   ├── playbooks/
│   │   ├── provision-base.yml           # Golden image bake (run once via SSH)
│   │   ├── provision-client.yml         # Per-client config (run via user-data)
│   │   ├── provision.yml                # Legacy: full provision over SSH
│   │   └── update.yml                   # Roll out OpenClaw package updates
│   └── roles/
│       ├── common/                      # System hardening, apt upgrades, timezone
│       ├── openclaw/                    # Service config, gateway token, systemd
│       ├── backup/                      # Hourly S3 sync + CloudWatch metrics
│       └── memory/                      # 3-layer memory: Markdown + ChromaDB + NetworkX
├── scripts/
│   ├── bootstrap.sh                     # First-time setup
│   ├── add-agent.sh                     # Register a new client in SSM
│   ├── bake-snapshot.sh                 # Build golden Lightsail snapshot
│   ├── publish-ansible.sh               # Sync playbooks to S3
│   ├── pause.sh                         # Snapshot + destroy instance
│   ├── resume.sh                        # Recreate instance from pause snapshot
│   ├── ssm-run.sh                       # Run a shell command via SSM
│   └── check-costs.py                   # AWS cost breakdown by service
├── tofu/
│   ├── main.tf                          # Client module instantiation
│   ├── clients.tf                       # SSM → local.clients
│   ├── variables.tf                     # Input variables
│   ├── providers.tf                     # AWS provider (primary + backup region)
│   ├── backend.tf                       # S3 backend config
│   ├── keys.tf                          # Lightsail SSH key pair
│   ├── alerts.tf                        # SNS + CloudWatch + Bedrock budgets
│   ├── outputs.tf                       # Instance IPs, bucket names
│   ├── terraform.tfvars.example         # Variable template
│   └── modules/client/                  # Per-client resource module
│       ├── main.tf                      # S3, IAM, SSM, Lightsail, firewall
│       ├── variables.tf                 # Module inputs
│       └── outputs.tf                   # Module outputs
└── LIFECYCLE.md                         # Detailed pause/resume lifecycle notes
```

---

## Instance Lifecycle

```
bootstrap.sh + add-agent.sh
        |
        v
bake-snapshot.sh  →  golden snapshot (clawless-golden-<ts>)
        |
        v
tofu apply  →  instance created from snapshot
               |
               v
          user-data runs ansible-playbook provision-client.yml
               |
               v
          /var/lib/openclaw/.provisioned written
          OpenClaw gateway listening on loopback:18789
               |
        [running]
               |
        pause.sh  →  pause snapshot + instance destroyed
               |
        resume.sh →  instance recreated from pause snapshot
                      workspace restored from S3
                      .provisioned present → ansible skipped
```

---

## Troubleshooting

**Check if an instance is provisioned:**
```bash
./scripts/ssm-run.sh --slug <slug> "ls -la /var/lib/openclaw/.provisioned"
```

**View provision logs:**
```bash
./scripts/ssm-run.sh --slug <slug> "cat /var/log/cloud-init-output.log"
```

**Check OpenClaw service:**
```bash
./scripts/ssm-run.sh --slug <slug> "systemctl status openclaw && journalctl -u openclaw -n 50"
```

**Check backup status:**
```bash
./scripts/ssm-run.sh --slug <slug> "systemctl status clawless-backup.timer"
```

**SSM instance not appearing:** Wait 2–3 minutes after instance creation. If still missing, check that the SSM activation hasn't expired (`tofu apply` creates a new one on each apply).

**Name collision on replace (`names are already in use`):** Lightsail `delete-instance` is asynchronous. The destroy provisioner in `main.tf` polls until the name is released before proceeding.

---

## Contributing

Feature requests, bug reports, and patches are welcome. Open an issue or pull request on GitHub.
