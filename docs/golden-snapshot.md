# Golden Snapshot

## Two-phase provisioning

Instance provisioning is split into two phases to minimize boot time for new agents:

1. **Bake (once)**: `bake-snapshot.sh` creates a temporary Lightsail instance, runs `provision-base.yml` to install all slow dependencies, then snapshots it. If a previous golden snapshot exists, the bake instance starts from it (incremental bake — most tasks skip via idempotency guards). Otherwise it starts from the base Ubuntu 24.04 blueprint. The resulting golden snapshot is reused for all agents.

2. **Provision (per agent)**: When a new agent is created, its Lightsail instance boots from the golden snapshot. The user-data script runs `provision-client.yml` which only does fast, client-specific configuration — writing credentials, seeding workspace files, and starting services.

## What's baked in

The golden snapshot includes everything that doesn't vary per client:

- System updates (`apt dist-upgrade`)
- Node.js 22 LTS
- OpenClaw (`npm install -g openclaw@latest`)
- `openclaw onboard` (creates systemd service and bare config)
- Docker + `openclaw-sandbox-common:bookworm-slim` image
- SearXNG (git clone, Python venv, pip packages, systemd unit)
- Memory system (Python venv, ChromaDB, sentence-transformers, NetworkX)
- SentenceTransformer model weights (pre-cached at bake time)
- Ansible playbooks at `/opt/clawless/ansible/` (only client-facing files: `provision-client.yml`, `ansible.cfg`, and each role's `client.yml`, defaults, and templates)
- `ansible-core` (so instances can self-provision via user-data)
- SearXNG ClawHub skill (promoted to bundled skills directory)
- Bash aliases (`checkclaw`, `checkboot`, `reprovision`)

## What's configured at boot

`provision-client.yml` handles only client-specific tasks:

- SearXNG settings (secret key, `settings.yml`)
- `credential_process` helper (needs IAM role ARN)
- AWS region in env file
- Gateway token generation
- OpenClaw service start
- Backup script (needs S3 bucket and agent slug)
- Workspace seed files (MEMORY.md, AGENTS.md, IDENTITY.md, etc.)
- OpenClaw config patching (model, channels, memory, search)
- Initial memory index
- Reindex timer

## When to rebake

Rebake whenever you change something that's part of the base image:

- System package updates (security patches)
- New or updated Ansible roles in `tasks/base.yml`
- OpenClaw version bump
- Docker sandbox image changes
- New Python packages in the memory venv
- Changes to `provision-base.yml` or the `common` role

You do **not** need to rebake for:

- Client-specific Ansible changes (`tasks/client.yml`)
- OpenClaw config changes (model, channels)
- Template changes (MEMORY.md.j2, etc.)
- Lambda or tofu changes

For client-side Ansible changes without rebaking, run `reprovision` on the instance — it clones the repo at the `/clawless/version` ref and re-runs `provision-client.yml`. New agents pick up changes automatically (user-data clones from git at boot).

## Bake process

`bake-snapshot.sh` does the following:

1. Generates an SSH key pair at `~/.ssh/clawless_ansible` if absent, uploads to Lightsail
3. Creates a temporary Lightsail instance from the previous golden snapshot (if available) or the base blueprint
4. Opens port 22 to the provisioner's IP only
5. Runs `provision-base.yml` via SSH
6. Clears SSM registration (so new instances register with their own activation)
7. Stops the instance and takes a snapshot (`clawless-golden-<timestamp>`)
8. Writes `golden_snapshot_name` to `terraform.tfvars` and uploads to S3
9. Runs `tofu apply` to register the new snapshot name in state
10. Cleans up the temporary instance (via trap)

Port 22 is only open on the temporary bake instance. Production agent instances have no inbound ports open — admin access is via SSM Session Manager.
