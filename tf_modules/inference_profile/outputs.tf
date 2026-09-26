output "arn" {
  description = "Profile ARN. This is the value agents receive as SONNET5_INFERENCE_PROFILE_ARN."
  value       = aws_bedrock_inference_profile.this.inference_profile_arn
}

output "id" {
  description = "Profile id."
  value       = aws_bedrock_inference_profile.this.inference_profile_id
}

output "name" {
  description = "Profile name."
  value       = aws_bedrock_inference_profile.this.name
}

output "model_source_arn" {
  description = "What the profile wraps, after the ARN was resolved."
  value       = local.source_arn
}

output "models" {
  description = "Model ARNs the profile routes to. A cross-region source lists one per region, which is what a narrowed IAM policy needs."
  value       = aws_bedrock_inference_profile.this.models
}
