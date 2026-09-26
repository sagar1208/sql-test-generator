variable "region" {
  description = "Region for the shared resources. Bedrock model access is granted per region, so this is the region the agents call."
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

variable "name_prefix" {
  description = "Platform prefix for shared resource names."
  type        = string
  default     = "tmnl-tina"
}

variable "sonnet_model_id" {
  description = <<-EOT
    Model the shared inference profile wraps. A regional prefix makes it a
    system-defined cross-region profile, which is what keeps a burst of large
    sources from being throttled.
  EOT
  type        = string
  default     = "eu.anthropic.claude-sonnet-4-5-20250929-v1:0"
}

variable "cost_center" {
  description = "Cost centre tag. The reason application inference profiles exist is to make this attributable."
  type        = string
  default     = "bi-platform"
}

variable "tags" {
  description = "Extra tags merged into the defaults."
  type        = map(string)
  default     = {}
}
