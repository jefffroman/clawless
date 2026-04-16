# Troubleshooting

## Task status

**Check if the ECS service is running:**
```bash
aws ecs describe-services --cluster clawless --services clawless-<client>-<agent> \
  --query 'services[0].{status:status,desired:desiredCount,running:runningCount}' \
  --region us-east-1
```

**View recent task events (scheduling failures, OOM, etc.):**
```bash
aws ecs describe-services --cluster clawless --services clawless-<client>-<agent> \
  --query 'services[0].events[:5]' --region us-east-1
```

## Container logs

Gateway logs go to CloudWatch at `/ecs/clawless-<client>-<agent>`:

```bash
aws logs tail /ecs/clawless-<client>-<agent> --since 1h --region us-east-1
```

**Follow logs in real time:**
```bash
aws logs tail /ecs/clawless-<client>-<agent> --follow --region us-east-1
```

## Broken sessions

If OpenClaw fails with "conversation must start with user message" or "user messages cannot contain reasoning content", the session history is corrupted. Force a new deployment to start from the last synced workspace state:

```bash
aws ecs update-service --cluster clawless --service clawless-<client>-<agent> \
  --force-new-deployment --region us-east-1
```

If sessions keep corrupting, clear them from S3 before the next boot:

```bash
aws s3 rm s3://clawless-backups-<account>/agents/<client>/<agent>/workspace/.openclaw/agents/main/sessions/ \
  --recursive --region us-east-1
```

Then force a new deployment as above.

## Lifecycle Lambda

**Check recent invocations:**
```bash
aws logs tail /aws/lambda/clawless-lifecycle --since 1h --region us-east-1
```

**Check for error flags blocking an agent:**
```bash
aws ssm get-parameter --name "/clawless/clients/<client>/<agent>/error" --region us-east-1
```

**Clear error flag and retry:**
```bash
aws ssm delete-parameter --name "/clawless/clients/<client>/<agent>/error" --region us-east-1
./scripts/wake-agent.sh <client>-<agent>
```

## Credentials

**Verify the task role is working (from container logs):**

Look for `install_aws_creds: credential_process wired at ~/.aws/config` in the boot logs. If absent, the `AWS_CONTAINER_CREDENTIALS_RELATIVE_URI` env var was not available at boot.

**Force a fresh task (picks up any IAM policy changes):**
```bash
aws ecs update-service --cluster clawless --service clawless-<client>-<agent> \
  --force-new-deployment --region us-east-1
```
