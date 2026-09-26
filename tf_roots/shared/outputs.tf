output "inference_profile_arn" {
  description = "Feeds SONNET5_INFERENCE_PROFILE_ARN in every agent root."
  value       = module.inference_profile.arn
}

output "inference_profile_id" {
  description = "Shared inference profile id."
  value       = module.inference_profile.id
}

output "inference_profile_models" {
  description = "Model ARNs the profile routes to. An agent root narrowing its own policy needs these."
  value       = module.inference_profile.models
}

output "invoke_shared_model_policy_arn" {
  description = "Managed policy to attach to an agent execution role instead of writing a Bedrock statement per agent."
  value       = aws_iam_policy.invoke_shared_model.arn
}

output "state_bucket" {
  description = "Bucket agent roots point their S3 backend at."
  value       = aws_s3_bucket.state.id
}

output "region" {
  description = "Region these shared resources live in."
  value       = var.region
}
