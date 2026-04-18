<img src="clawless-logo.png" alt="Clawless" width="80" align="left">

# Clawless

**An on-demand, resumable, serverless OpenClaw platform for AWS.**

> **Alpha** — Clawless is under active development. The core lifecycle (add, sleep, wake, remove) is working and tested, but the project is early. Expect rough edges, breaking changes, and missing features. Feedback and contributions are welcome.

<br clear="left">

Clawless provisions isolated [OpenClaw](https://openclaw.ai) agent gateways on AWS Fargate. Each agent gets its own ECS service, IAM task role, Bedrock access, and workspace backed up to S3. Services can be scaled to zero when idle and resumed in seconds — paying only for storage when sleeping.

---

## Architecture Overview

```
SSM Parameter Store (/clawless/clients/{client}/{agent})
        |
        v
Step Functions → DynamoDB (pending) → Lifecycle Lambda
        |
        +-- per-agent ECS Fargate service (one task, desired 0 or 1)
        |       |
        |       +-- gateway container (node:22-slim + openclaw)
        |       +-- entrypoint: sync-down S3 → configure → exec gateway
        |       +-- SIGTERM: sync-up workspace → exit
        |       +-- tools run in-process (OPENCLAW_SANDBOX_MODE=off)
        |
        +-- per-agent IAM task role (Bedrock, S3, CloudWatch, ECS self-stop)
        +-- shared S3 backup bucket (per-agent prefix, cross-region replication)
        +-- shared SearXNG Lambda (web search, one per region)
        +-- shared wake-listener Lambda (Telegram webhook receiver for sleeping agents)
```

- **Sleep/wake**: `ecs:UpdateService desired_count=0/1`. Container syncs workspace to S3 on SIGTERM; syncs back down on boot.
- **Channel-triggered wake**: When a sleeping agent receives a Telegram message, the wake listener Lambda queues it in DynamoDB, sets `/active=true`, and triggers the lifecycle SFN. The gateway replays the queued message on boot.
- **No SSH, no instances**: Fargate tasks are ephemeral. Debug via CloudWatch logs or `aws ecs execute-command`.
- **Agent memory**: Human-editable Markdown with vector search (sentence-transformers, ChromaDB) — auto-reindexed.
- **Web search**: Shared SearXNG Lambda — no per-agent process, no API keys.
- **Lifecycle automation**: All agent operations driven by a single Step Functions invocation. See [docs/lifecycle.md](docs/lifecycle.md).

---

## Terminology

- **Client**: the human customer (e.g. "Acme Corp"). One client may have multiple agents.
- **Agent**: one OpenClaw gateway serving a client. Identified by `{client-slug}/{agent-slug}` (SSM path) or `{client-slug}-{agent-slug}` (AWS resource names).

---

## Prerequisites

### Tools

| Tool | Version | macOS | Ubuntu |
|------|---------|-------|--------|
| [OpenTofu](https://opentofu.org/docs/intro/install/) | >= 1.10 | `brew install opentofu` | `snap install opentofu --classic` |
| [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) | >= 2.x | `brew install awscli` | official Linux installer (see link) |
| [Docker](https://docs.docker.com/get-docker/) | >= 24 | [Docker Desktop](https://www.docker.com/products/docker-desktop/) | `apt install docker.io` |
| Python 3 | >= 3.10 | `brew install python` | `apt install python3` |
| jq | any | `brew install jq` | `apt install jq` |

### AWS Credentials

You need an IAM user or role with the following permissions:

```
s3:*          iam:*       ssm:*       ecs:*
sns:*         cloudwatch:*  budgets:*   ecr:*
states:*      lambda:*      dynamodb:*  bedrock:InvokeModel
logs:*        ec2:* (VPC/SG management)
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

### 2. Initialize OpenTofu and Apply

```bash
cd tofu
tofu init -backend-config=backend.hcl
tofu apply
```

### 3. Build and Push Container Images

```bash
./scripts/build-lambda.sh
./scripts/build-gateway-image.sh
./scripts/build-searxng-image.sh
```

### 4. Add Your First Agent

```bash
./scripts/add-agent.sh
```

Prompts for client name, agent name, channel type, and bot credentials. The script invokes Step Functions which writes the agent config to SSM and triggers the Lifecycle Lambda.

Verify the agent is running:

```bash
aws ecs describe-services --cluster clawless --services clawless-<client>-<agent> \
  --query 'services[0].{status:status,desired:desiredCount,running:runningCount}' \
  --region us-east-1
```

---

## Daily Operations

### Add / remove / sleep / wake

```bash
./scripts/add-agent.sh                                  # interactive prompts
./scripts/remove-agent.sh <client-slug> <agent-slug>    # archive workspace + full teardown
./scripts/sleep-agent.sh <client-slug>-<agent-slug>     # ECS desired=0 (~5s to dark)
./scripts/wake-agent.sh <client-slug>-<agent-slug>      # ECS desired=1 (~2 min to first message)
```

### Check logs

```bash
aws logs tail /ecs/clawless-<client>-<agent> --since 1h --region us-east-1
```

### Check costs

```bash
python3 scripts/check-costs.py 7    # Last 7 days
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
| [docs/ssm.md](docs/ssm.md) | Full SSM Parameter Store reference — paths, schemas, IAM scope |
| [docs/versioning.md](docs/versioning.md) | How `/clawless/version` controls Lambda behavior |
| [docs/lifecycle.md](docs/lifecycle.md) | Step Functions → SSM + DynamoDB → Lambda flow, per-slug ownership, race handling |
| [docs/credentials.md](docs/credentials.md) | Task roles, credential_process workaround, gateway tokens |
| [docs/backups.md](docs/backups.md) | S3 workspace sync, retention, restore procedures |
| [docs/troubleshooting.md](docs/troubleshooting.md) | ECS status, CloudWatch logs, broken sessions, Lambda debugging |

---

## Contributing

Issues and pull requests are welcome on [GitHub](https://github.com/jefffroman/clawless).
