output "runtime_arn" {
  description = "ARN of the agent runtime. This is what the invoker Lambda calls."
  value       = awscc_bedrockagentcore_runtime.this.agent_runtime_arn
}

output "runtime_id" {
  description = "AgentCore runtime id."
  value       = awscc_bedrockagentcore_runtime.this.agent_runtime_id
}

output "runtime_name" {
  description = "Runtime name as AgentCore sees it, underscores and all."
  value       = local.runtime_name
}

output "runtime_version" {
  description = "Version created by this apply. Qualify an invocation with it to pin a rollout."
  value       = awscc_bedrockagentcore_runtime.this.agent_runtime_version
}

output "execution_role_arn" {
  description = "Execution role assumed by the runtime."
  value       = aws_iam_role.execution.arn
}

output "execution_role_name" {
  description = "Execution role name, for attaching an extra policy from a root."
  value       = aws_iam_role.execution.name
}

output "ecr_repository_url" {
  description = "Repository to push the ARM64 image to."
  value       = aws_ecr_repository.this.repository_url
}

output "ecr_repository_arn" {
  description = "Repository ARN."
  value       = aws_ecr_repository.this.arn
}

output "image_uri" {
  description = "Exact image reference the runtime is configured with."
  value       = local.container_uri
}

output "log_group_name" {
  description = "CloudWatch log group carrying the agent's own logs."
  value       = aws_cloudwatch_log_group.runtime.name
}

output "memory_id" {
  description = "AgentCore Memory store id, or \"\" when the agent runs memoryless."
  value       = local.memory_id
}

output "memory_arn" {
  description = "AgentCore Memory store ARN, or \"\" when no store was created."
  value       = local.memory_enabled ? awscc_bedrockagentcore_memory.this[0].memory_arn : ""
}

output "environment" {
  description = "Environment variables actually sent to the container, after empties were dropped."
  value       = local.environment
}
