# ── SNS Topic ─────────────────────────────────────────────────────────────────

resource "aws_sns_topic" "alerts" {
  name = "clawless-alerts"
  tags = var.tags
}

data "aws_iam_policy_document" "sns_alerts" {
  statement {
    effect  = "Allow"
    actions = ["SNS:Publish"]
    principals {
      type        = "Service"
      identifiers = ["budgets.amazonaws.com"]
    }
    resources = [aws_sns_topic.alerts.arn]
  }
}

resource "aws_sns_topic_policy" "alerts" {
  arn    = aws_sns_topic.alerts.arn
  policy = data.aws_iam_policy_document.sns_alerts.json
}

resource "aws_sns_topic_subscription" "alerts_email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# ── Backup Failure Alarms ─────────────────────────────────────────────────────
# One alarm per agent. The backup script publishes BackupFailure=1 on failure,
# BackupFailure=0 on success. Missing data is treated as OK — if the instance
# is inactive, no metrics are expected.

resource "aws_cloudwatch_metric_alarm" "backup_failure" {
  for_each = local.agents

  alarm_name        = "clawless-${replace(each.key, "/", "-")}-backup-failure"
  alarm_description = "Workspace backup to S3 failed for agent ${each.key}"
  namespace         = "Clawless/Backup"
  metric_name       = "BackupFailure"

  dimensions = {
    AgentSlug = replace(each.key, "/", "-")
  }

  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 2
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = var.tags
}

# ── Bedrock Budget ─────────────────────────────────────────────────────────────
# Aggregate across all clients — per-client tracking requires Application
# Inference Profiles and is deferred.

resource "aws_budgets_budget" "bedrock" {
  name         = "clawless-bedrock-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.bedrock_monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  cost_filter {
    name   = "Service"
    values = ["Amazon Bedrock"]
  }

  notification {
    comparison_operator       = "GREATER_THAN"
    threshold                 = 80
    threshold_type            = "PERCENTAGE"
    notification_type         = "ACTUAL"
    subscriber_sns_topic_arns = [aws_sns_topic.alerts.arn]
  }

  notification {
    comparison_operator       = "GREATER_THAN"
    threshold                 = 100
    threshold_type            = "PERCENTAGE"
    notification_type         = "ACTUAL"
    subscriber_sns_topic_arns = [aws_sns_topic.alerts.arn]
  }
}
