# ── Golden Snapshot Cleanup ───────────────────────────────────────────────────
# Nightly Lambda that prunes old golden snapshots. Retention policy:
#   - Keep everything newer than 5 days
#   - Keep the most recent 5 regardless of age
# Both conditions are unioned — a snapshot survives if either applies.

data "archive_file" "snapshot_cleanup" {
  type        = "zip"
  output_path = "${path.module}/.terraform/tmp/snapshot-cleanup.zip"

  source {
    content  = <<-PYTHON
import boto3
from datetime import datetime, timezone, timedelta

lightsail = boto3.client("lightsail")
KEEP_DAYS = 5
KEEP_COUNT = 5
PREFIX = "clawless-golden-"

def lambda_handler(event, context):
    snapshots = []
    resp = lightsail.get_instance_snapshots()
    snapshots.extend(resp["instanceSnapshots"])
    while "nextPageToken" in resp:
        resp = lightsail.get_instance_snapshots(pageToken=resp["nextPageToken"])
        snapshots.extend(resp["instanceSnapshots"])

    golden = [s for s in snapshots if s["name"].startswith(PREFIX)]
    golden.sort(key=lambda s: s["createdAt"], reverse=True)

    cutoff = datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)
    recent_names = {s["name"] for s in golden[:KEEP_COUNT]}

    to_delete = []
    for snap in golden:
        if snap["name"] in recent_names:
            continue
        if snap["createdAt"] >= cutoff:
            continue
        to_delete.append(snap)

    for snap in to_delete:
        print(f"Deleting {snap['name']} (created {snap['createdAt'].isoformat()})")
        lightsail.delete_instance_snapshot(instanceSnapshotName=snap["name"])

    print(f"Kept {len(golden) - len(to_delete)}, deleted {len(to_delete)}")
    return {"kept": len(golden) - len(to_delete), "deleted": len(to_delete)}
    PYTHON
    filename = "handler.py"
  }
}

resource "aws_iam_role" "snapshot_cleanup" {
  name = "clawless-snapshot-cleanup"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })

  tags = var.tags
}

resource "aws_iam_role_policy" "snapshot_cleanup" {
  name = "clawless-snapshot-cleanup"
  role = aws_iam_role.snapshot_cleanup.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["lightsail:GetInstanceSnapshots", "lightsail:DeleteInstanceSnapshot"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:*"
      },
    ]
  })
}

resource "aws_lambda_function" "snapshot_cleanup" {
  function_name    = "clawless-snapshot-cleanup"
  role             = aws_iam_role.snapshot_cleanup.arn
  handler          = "handler.lambda_handler"
  runtime          = "python3.12"
  timeout          = 60
  filename         = data.archive_file.snapshot_cleanup.output_path
  source_code_hash = data.archive_file.snapshot_cleanup.output_base64sha256

  tags = var.tags
}

resource "aws_cloudwatch_event_rule" "snapshot_cleanup" {
  name                = "clawless-snapshot-cleanup-nightly"
  description         = "Nightly golden snapshot cleanup"
  schedule_expression = "cron(0 6 * * ? *)" # 06:00 UTC daily

  tags = var.tags
}

resource "aws_cloudwatch_event_target" "snapshot_cleanup" {
  rule      = aws_cloudwatch_event_rule.snapshot_cleanup.name
  target_id = "snapshot-cleanup-lambda"
  arn       = aws_lambda_function.snapshot_cleanup.arn
}

resource "aws_lambda_permission" "snapshot_cleanup" {
  statement_id  = "AllowEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.snapshot_cleanup.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.snapshot_cleanup.arn
}
