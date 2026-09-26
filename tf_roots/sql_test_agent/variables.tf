variable "region" {
  description = "Region to deploy into. Must be the region Bedrock model access was granted in."
  type        = string
  default     = "eu-central-1"
}

variable "environment" {
  description = "Deployment environment: dev, acc or prd."
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev", "acc", "prd"], var.environment)
    error_message = "environment must be dev, acc or prd."
  }
}

variable "image_tag" {
  description = <<-EOT
    Image tag to deploy. `latest` is fine for dev; a release tag or an immutable
    digest is what a promotion should pin, because AgentCore resolves the tag at
    CreateAgentRuntime and never looks again.
  EOT
  type        = string
  default     = ""
}

variable "inference_profile_arn" {
  description = <<-EOT
    Inference profile to use. Leave empty to read it from the shared root's
    remote state; set it to apply against an existing profile without wiring
    remote state at all.
  EOT
  type        = string
  default     = ""
}

variable "shared_state" {
  description = <<-EOT
    Where tf_roots/shared keeps its state. Leave null when passing
    inference_profile_arn directly.
  EOT
  type = object({
    bucket = string
    key    = optional(string, "tmnl-tina-pipeline-bi-agents/shared/terraform.tfstate")
    region = optional(string, "eu-central-1")
  })
  default = null
}

variable "log_retention_days" {
  description = "CloudWatch retention for the agent runtime and the invoker Lambda."
  type        = number
  default     = 30
}

variable "lambda_memory_mib" {
  description = <<-EOT
    Invoker Lambda memory. The function waits on a network call and parses one
    JSON document, so this buys nothing beyond a faster cold start.
  EOT
  type        = number
  default     = 512
}

variable "create_function_url" {
  description = <<-EOT
    Expose the invoker through a Lambda function URL with IAM auth. Convenient
    for a smoke test; an API Gateway in front of it is what a real client should
    get, so this defaults to off.
  EOT
  type        = bool
  default     = false
}

variable "cost_center" {
  description = "Cost centre tag, matched with the shared inference profile so model spend reconciles."
  type        = string
  default     = "bi-platform"
}

variable "tags" {
  description = "Extra tags merged into the defaults."
  type        = map(string)
  default     = {}
}
