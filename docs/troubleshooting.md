# Troubleshooting

## Instance status

**Check if provisioned:**
```bash
./scripts/ssm-run.sh --slug <client>-<agent> "ls -la /home/ubuntu/.openclaw/.provisioned"
```

**View provision logs:**
```bash
./scripts/ssm-run.sh --slug <client>-<agent> "cat /var/log/cloud-init-output.log"
```

## OpenClaw service

The gateway runs as a user-level systemd service under the `ubuntu` user:

```bash
./scripts/ssm-run.sh --slug <client>-<agent> \
  "sudo -u ubuntu XDG_RUNTIME_DIR=/run/user/\$(id -u ubuntu) systemctl --user status openclaw-gateway"
```

```bash
./scripts/ssm-run.sh --slug <client>-<agent> \
  "sudo -u ubuntu XDG_RUNTIME_DIR=/run/user/\$(id -u ubuntu) journalctl --user-unit openclaw-gateway -n 50"
```

Or use the `checkclaw` alias if logged in via SSM session:
```bash
checkclaw
```

## Broken sessions

If OpenClaw fails with "conversation must start with user message" or "user messages cannot contain reasoning content", the session history is corrupted:

```bash
./scripts/ssm-run.sh --slug <client>-<agent> \
  "rm -f /home/ubuntu/.openclaw/agents/main/sessions/*.jsonl && \
   echo '{}' > /home/ubuntu/.openclaw/agents/main/sessions/sessions.json && \
   sudo -u ubuntu XDG_RUNTIME_DIR=/run/user/\$(id -u ubuntu) systemctl --user restart openclaw-gateway"
```

## Backup

```bash
./scripts/ssm-run.sh --slug <client>-<agent> "systemctl status clawless-backup.timer"
```

## SearXNG

```bash
./scripts/ssm-run.sh --slug <client>-<agent> \
  "systemctl status searxng && curl -s http://127.0.0.1:8080/search?q=test&format=json | head -c 200"
```

## Lifecycle Lambda

**Check recent invocations:**
```bash
aws logs tail /aws/lambda/clawless-lifecycle --since 1h --region us-east-1
```

**Check for error flags blocking an agent:**
```bash
aws ssm get-parameter --name "/clawless/clients/<client>/<agent>/error" --region us-east-1
```

**Clear error flag and retry** (pause then resume to trigger the lifecycle Lambda):
```bash
aws ssm delete-parameter --name "/clawless/clients/<client>/<agent>/error" --region us-east-1
./scripts/pause-agent.sh <client> <agent>
./scripts/resume-agent.sh <client> <agent>
```

## Credentials

**Verify credential_process works on an instance:**
```bash
./scripts/ssm-run.sh --slug <client>-<agent> \
  "sudo /usr/local/sbin/clawless-creds-helper | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d[\"Expiration\"])'"
```

**Check if IMDS is disabled:**
```bash
./scripts/ssm-run.sh --slug <client>-<agent> "grep EC2_METADATA /etc/openclaw/openclaw.env"
```

## Re-provisioning a running instance

If you need to re-run Ansible on a running instance after pushing playbook changes:

```bash
./scripts/ssm-run.sh --slug <client>-<agent> "reprovision"
```

The `reprovision` alias clones the repo at the `/clawless/version` ref and re-runs `provision-client.yml` with the client vars from `/opt/clawless/client-vars.json`.
