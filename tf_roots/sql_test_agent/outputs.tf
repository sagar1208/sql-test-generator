output "agent_runtime_arn" {
  description = "ARN of the deployed agent runtime."
  value       = module.agent.runtime_arn
}

output "agent_runtime_version" {
  description = "Runtime version created by this apply."
  value       = module.agent.runtime_version
}

output "agent_execution_role_arn" {
  description = "Role the runtime assumes."
  value       = module.agent.execution_role_arn
}

output "ecr_repository_url" {
  description = "Push the ARM64 image here before the first apply."
  value       = module.agent.ecr_repository_url
}

output "image_uri" {
  description = "Exact image the runtime is pinned to."
  value       = module.agent.image_uri
}

output "agent_log_group" {
  description = "Where the agent's own logs land."
  value       = module.agent.log_group_name
}

output "memory_id" {
  description = "AgentCore Memory store id, or \"\" when memory is disabled."
  value       = module.agent.memory_id
}

output "agent_environment" {
  description = "Environment variables the container receives, after empties were dropped. The quickest way to see what the manifest resolved to."
  value       = module.agent.environment
}

output "invoker_function_name" {
  description = "Invoker Lambda to call."
  value       = aws_lambda_function.invoker.function_name
}

output "invoker_function_arn" {
  description = "Invoker Lambda ARN, for an API Gateway integration or a Step Functions task."
  value       = aws_lambda_function.invoker.arn
}

output "invoker_log_group" {
  description = "Structured invoker logs, one JSON line per event."
  value       = aws_cloudwatch_log_group.invoker.name
}

output "invoker_function_url" {
  description = "Function URL, when create_function_url is true. IAM-authenticated."
  value       = try(aws_lambda_function_url.invoker[0].function_url, "")
}

output "inference_profile_arn" {
  description = "Profile this agent was wired to, whether passed in or read from shared state."
  value       = local.inference_profile_arn
}
