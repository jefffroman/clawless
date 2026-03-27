<img src="clawless-logo.png" alt="Clawless" width="80" align="left">

# Clawless

**An on-demand, resumable, serverless OpenClaw platform for AWS.**

> **Alpha** — Clawless is under active development. The core lifecycle (add, pause, resume, remove) is working and tested, but the project is early. Expect rough edges, breaking changes, and missing features. Feedback and contributions are welcome.

<br clear="left">

Clawless provisions isolated [OpenClaw](https://openclaw.ai) agent instances on AWS Lightsail. Each agent gets its own Lightsail instance, IAM role, Bedrock access, and workspace backed up to S3. Instances can be paused (snapshotted) when idle and resumed in minutes — paying only for storage when paused.

---

## Architecture Overview

```
SSM Parameter Store (/clawless/clients/{client}/{agent})
        |
        v
Step Functions → DynamoDB (pending) → Lifecycle Lambda (tofu apply)
        |
        +-- per-agent Lightsail instance (from golden snapshot)
        |       |
        |       +-- user-data: ansible-playbook provision-client.yml (local)
        |       +-- hourly: aws s3 sync workspace → S3 backup bucket
        |       |
        |       +-- ubuntu user: runs OpenClaw gateway (user-level systemd)
        |       +-- sandbox: tool execution in Docker (openclaw-sandbox-common)
        |       +-- SearXNG: local web search (loopback, no API keys)
        |
        +-- per-agent IAM role (Bedrock, S3, CloudWatch)
        +-- per-agent SSM Hybrid Activation (temporary rotating creds)
        +-- shared S3 backup bucket (per-agent prefix, cross-region replication)
```

- **Self-provisioning**: Instances configure themselves at boot via user-data (no inbound SSH required after bake).
- **Admin access**: Via AWS SSM Session Manager — no port 22 open on production instances.
- **Pause/resume**: Snapshot → destroy instance → restore from snapshot. Workspace persists in S3.
- **Sandbox isolation**: Tool execution runs in a Docker container as the `ubuntu` user. The gateway manages the container; tools never run on the host directly.
- **Agent memory**: 3-layer system — human-editable Markdown, ChromaDB vector search, NetworkX knowledge graph — auto-reindexed every 5 minutes.
- **Web search**: Self-hosted SearXNG on each instance — no API keys, no per-query costs.
- **Lifecycle automation**: All agent operations are driven by a single Step Functions invocation that writes to SSM, records the event in DynamoDB, and invokes the Lifecycle Lambda. See [docs/lifecycle.md](docs/lifecycle.md).

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
lightsail:*    s3:*          iam:*       ssm:*
sns:*          cloudwatch:*  budgets:*   ecr:*
states:*       lambda:*      dynamodb:*  bedrock:InvokeModel
ce:GetCostAndUsage (for check-costs.py)
```

Configure credentials before running any scripts:

```bash
aws configure
# or
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_DEFAULT_REGION=us-east-1
```

---

## First-Time Setup

### 1. Bootstrap

```bash
./scripts/bootstrap.sh
```

Prompts for AWS region and alert email. Creates the S3 state bucket, local config files, and sets `/clawless/version` to the current git ref. See [docs/versioning.md](docs/versioning.md).

### 2. Initialize OpenTofu

```bash
cd tofu
tofu init -backend-config=backend.hcl
```

### 3. Bake the Golden Snapshot

```bash
./scripts/bake-snapshot.sh
```

Bake once before adding your first agent. Re-bake when system packages or base playbooks change. Takes ~15 minutes. See [docs/golden-snapshot.md](docs/golden-snapshot.md).

### 4. Add Your First Agent

```bash
./scripts/add-agent.sh
```

Prompts for client name, agent name, channel type, and bot credentials. The script invokes Step Functions which writes the agent config to SSM and triggers the Lifecycle Lambda — boot-to-ready is ~8 minutes.

Verify the agent is provisioned and running:

```bash
./scripts/ssm-run.sh --slug <client>-<agent> "checkboot"
./scripts/ssm-run.sh --slug <client>-<agent> "checkclaw"
```

---

## Daily Operations

### Run a command on an instance

```bash
./scripts/ssm-run.sh --slug <client>-<agent> "<command>"
```

Each instance has convenience aliases: `checkclaw` (service status + recent logs), `checkboot` (provision status + cloud-init log), and `reprovision` (sync latest playbooks from S3 and re-run).

### Add / remove / pause / resume

```bash
./scripts/add-agent.sh
./scripts/remove-agent.sh <client-slug> <agent-slug>
./scripts/pause-agent.sh <client-slug> <agent-slug>
./scripts/resume-agent.sh <client-slug> <agent-slug>
```

### Check costs

```bash
python3 scripts/check-costs.py 7    # Last 7 days
```

### Update playbooks on a running instance

```bash
./scripts/publish-ansible.sh
./scripts/ssm-run.sh --slug <client>-<agent> "reprovision"
```

### Deploy infrastructure changes

```bash
cd tofu && tofu apply
```

See [docs/lifecycle.md](docs/lifecycle.md) for details on what runs locally vs. in the Lambda.

---

## Documentation

| Topic | Description |
|-------|-------------|
| [docs/versioning.md](docs/versioning.md) | How `/clawless/version` controls Lambda behavior |
| [docs/golden-snapshot.md](docs/golden-snapshot.md) | Two-phase provisioning, what's baked vs. configured at boot |
| [docs/lifecycle.md](docs/lifecycle.md) | Step Functions → SSM + DynamoDB → Lambda flow, per-slug ownership, race handling |
| [docs/credentials.md](docs/credentials.md) | Credential delivery, `credential_process`, gateway tokens, IMDS workaround |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Checking services, broken sessions, re-provisioning, Lambda debugging |

---

## Contributing

Issues and pull requests are welcome on [GitHub](https://github.com/jefffroman/clawless).
