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

Gateway logs go to CloudWatch at `/clawless/fargate/<client>-<agent>`:

```bash
aws logs tail /clawless/fargate/<client>-<agent> --since 1h --region us-east-1
```

**Follow logs in real time:**
```bash
aws logs tail /clawless/fargate/<client>-<agent> --follow --region us-east-1
```

**Enable verbose (DEBUG-level) gateway logs without redeploying:**
```bash
aws ssm put-parameter \
  --name "/clawless/clients/<client>/<agent>/verbose" \
  --type String --value true --overwrite --region us-east-1
./scripts/sleep-agent.sh <client> <agent>   # entrypoint reads /verbose at boot
./scripts/wake-agent.sh  <client> <agent>
```

## Broken transcripts

If the gateway crashes mid-turn or you see Bedrock errors about message ordering ("toolUse without matching toolResult", etc.), archive the agent's transcripts so the next boot starts fresh:

```bash
aws s3 mv s3://clawless-backups-<account>/agents/<client>/<agent>/workspace/transcripts/ \
  s3://clawless-backups-<account>/agents/<client>/<agent>/workspace/transcripts.bak-$(date +%s)/ \
  --recursive --region us-east-1
```

Then force a new deployment so the gateway syncs the cleaned workspace:

```bash
aws ecs update-service --cluster clawless --service clawless-<client>-<agent> \
  --force-new-deployment --region us-east-1
```

The agent's `memory/` files are untouched — only the per-peer session JSONLs are archived.

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

The gateway's boto3 picks up the task role automatically via the ECS metadata endpoint (`AWS_CONTAINER_CREDENTIALS_RELATIVE_URI`). If you see `AccessDeniedException` in the logs, the task role's IAM policy is missing the action — see `tofu/modules/client/main.tf` for the per-agent grants.

The agent's `bash` tool runs as a separate UID (`clawless-tool`) with the AWS credential env vars stripped, so it cannot inherit task-role auth. AWS-bound work (sleep, web_search) happens in-process via the gateway's own boto3 clients.

**Force a fresh task (picks up any IAM policy changes):**
```bash
aws ecs update-service --cluster clawless --service clawless-<client>-<agent> \
  --force-new-deployment --region us-east-1
```
