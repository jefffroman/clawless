# Versioning

## How it works

The Lifecycle Lambda clones the clawless repo every time it runs. The SSM parameter `/clawless/version` tells it which git ref to clone (`git clone --depth=1 --branch <ref>`).

`bootstrap.sh` sets this automatically to whatever git ref you're on when you run it:
- On a tag (e.g. `v0.4.1`): uses the tag name
- On an untagged commit: uses the short SHA

This ensures the Lambda always runs the same tofu code you bootstrapped with. There is no prompt or manual override at bootstrap time.

## Why not `latest`?

Earlier versions defaulted to a floating `latest` tag. This caused problems:

- Developers who cloned a specific release would silently get different tofu code if `latest` moved
- The Lambda's behavior could change without any action from the operator
- Debugging was harder because the running code didn't match the local checkout

The current design treats `/clawless/version` as a pinned deployment ref that matches your checkout.

## Updating the version

After pulling new code or checking out a different tag, update the SSM parameter to match:

```bash
# Set to a specific tag
aws ssm put-parameter --name /clawless/version \
  --value v0.5.0 --overwrite --region us-east-1

# Set to current git ref (same logic as bootstrap)
aws ssm put-parameter --name /clawless/version \
  --value "$(git describe --tags --exact-match 2>/dev/null || git rev-parse --short HEAD)" \
  --overwrite --region us-east-1
```

## Testing a branch

During development, you can point the Lambda at a branch to test tofu changes without tagging:

```bash
aws ssm put-parameter --name /clawless/version \
  --value feat/my-branch --overwrite --region us-east-1
```

Remember to set it back after testing. The Lambda clones `--branch`, which accepts tags, branches, and SHAs.

## What this controls

Only tofu code — the module definitions, variable files, and provider config that the Lambda runs via `tofu apply`. It does **not** affect:

- **Lambda handler code** (`lambda/handler.py`): baked into the container image, updated by `tofu apply` locally when `handler.py` or `Dockerfile` change
- **Ansible playbooks**: pulled from git at boot (user-data) and on `reprovision`, using the `/clawless/version` ref
- **Scripts** (`scripts/*`): run locally from your checkout, not from the Lambda
