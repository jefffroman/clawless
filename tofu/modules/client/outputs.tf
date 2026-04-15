output "service_name" {
  value = aws_ecs_service.gateway.name
}

output "service_arn" {
  value = aws_ecs_service.gateway.id
}

output "task_role_arn" {
  value = aws_iam_role.task.arn
}

output "log_group_name" {
  value = aws_cloudwatch_log_group.task.name
}

output "gateway_token_ssm_name" {
  value = aws_ssm_parameter.gateway_token.name
}
