variable "name" {
  description = "Inference profile name. Shown in Bedrock and in cost reports, so it should name the consumer, not the model."
  type        = string

  validation {
    condition     = can(regex("^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$", var.name))
    error_message = "Inference profile names are up to 64 characters of letters, digits, dot, underscore and hyphen."
  }
}

variable "description" {
  description = "What this profile is for."
  type        = string
  default     = ""
}

variable "model_id" {
  description = <<-EOT
    Foundation model id, or a system-defined (cross-region) inference profile id
    such as `eu.anthropic.claude-sonnet-4-5-20250929-v1:0`. Ignored when
    `model_source_arn` is set.

    A cross-region profile is usually the right source: it spreads a burst over
    several regions, which is what keeps a large source from being throttled
    halfway through a run.
  EOT
  type        = string
  default     = ""
}

variable "system_defined_profile" {
  description = "True when model_id names a system-defined inference profile rather than a foundation model. Regional prefixes (eu., us.) are system-defined."
  type        = bool
  default     = true
}

variable "model_source_arn" {
  description = "Full ARN to copy from, when neither ARN shape above fits. Takes precedence over model_id."
  type        = string
  default     = ""
}

variable "tags" {
  description = <<-EOT
    Tags on the profile. These are the reason application inference profiles
    exist: Bedrock attributes usage to the profile, so a tag here is how one
    agent's model spend is told apart from another's.
  EOT
  type        = map(string)
  default     = {}
}
