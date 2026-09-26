variable "name" {
  description = "Agent name, as declared by `name` in the agent's agent.yaml (for example sql_test_agent)."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9_]{2,40}$", var.name))
    error_message = "Agent names are lowercase letters, digits and underscores. AgentCore rejects hyphens in a runtime name."
  }
}

variable "name_prefix" {
  description = "Platform prefix applied to every resource this module creates."
  type        = string
  default     = "tmnl_tina"
}

variable "description" {
  description = "Human-readable purpose, shown in the AgentCore console."
  type        = string
  default     = ""
}

variable "platform" {
  description = "Container platform. AgentCore Runtime accepts linux/arm64 only."
  type        = string
  default     = "linux/arm64"

  validation {
    condition     = var.platform == "linux/arm64"
    error_message = "AgentCore Runtime rejects anything but linux/arm64 at CreateAgentRuntime. Build the image with --platform linux/arm64."
  }
}

variable "ecr_repository_name" {
  description = "ECR repository for the agent image, from `container.ecr_repository` in agent.yaml."
  type        = string
}

variable "image_tag" {
  description = "Image tag to deploy. A digest is preferable in production; a tag is what CI pushes."
  type        = string
  default     = "latest"
}

variable "environment" {
  description = <<-EOT
    Environment variables injected into the container, from `environment` in
    agent.yaml. Empty values are dropped, and MEMORY_ID /
    SONNET5_INFERENCE_PROFILE_ARN / BEDROCK_MODEL_ID are overwritten with the
    values this module resolves.
  EOT
  type        = map(string)
  default     = {}
}

variable "model_id" {
  description = "Bedrock model id to inject as BEDROCK_MODEL_ID. Leave empty when using an inference profile."
  type        = string
  default     = ""
}

variable "inference_profile_arn" {
  description = "Application inference profile ARN to inject as SONNET5_INFERENCE_PROFILE_ARN."
  type        = string
  default     = ""
}

variable "resources" {
  description = <<-EOT
    Declared sizing from agent.yaml. AgentCore Runtime is serverless and
    CreateAgentRuntime takes no sizing, so these are recorded as tags: they give
    cost reporting and the Fargate fallback path one source of truth instead of
    two.
  EOT
  type = object({
    cpu                   = number
    memory_mib            = number
    ephemeral_storage_mib = optional(number, 512)
  })
  default = {
    cpu        = 1024
    memory_mib = 2048
  }
}

variable "timeouts" {
  description = "Request and session-idle timeouts, in seconds, from agent.yaml."
  type = object({
    request_seconds      = optional(number, 900)
    session_idle_seconds = optional(number, 1800)
  })
  default = {}

  validation {
    condition     = coalesce(var.timeouts.request_seconds, 900) >= 60
    error_message = "A request timeout under 60 seconds cannot complete one Bedrock call on a large source."
  }
}

variable "network_mode" {
  description = "AgentCore network mode: PUBLIC, or VPC once a VPC configuration is supplied."
  type        = string
  default     = "PUBLIC"

  validation {
    condition     = contains(["PUBLIC", "VPC"], var.network_mode)
    error_message = "network_mode must be PUBLIC or VPC."
  }
}

variable "server_protocol" {
  description = "Runtime protocol. HTTP serves POST /invocations and GET /ping; MCP exposes the agent as a tool server."
  type        = string
  default     = "HTTP"

  validation {
    condition     = contains(["HTTP", "MCP", "A2A"], var.server_protocol)
    error_message = "server_protocol must be HTTP, MCP or A2A."
  }
}

variable "observability_enabled" {
  description = "Emit OTEL traces to CloudWatch. Cheap, and the only way to see a session's spans."
  type        = bool
  default     = true
}

variable "memory" {
  description = <<-EOT
    Short-term session memory, from `memory` in agent.yaml. With enabled = false
    no store is created and MEMORY_ID stays empty, which the agent treats as a
    supported memoryless mode.
  EOT
  type = object({
    enabled           = optional(bool, false)
    name              = optional(string, "")
    event_expiry_days = optional(number, 7)
    strategies        = optional(list(string), [])
  })
  default = {}

  validation {
    condition     = coalesce(var.memory.event_expiry_days, 7) >= 1
    error_message = "event_expiry_days must be at least 1."
  }
}

variable "bedrock_actions" {
  description = "Bedrock actions the execution role is granted, from `iam.bedrock_actions` in agent.yaml."
  type        = list(string)
  default     = ["bedrock:InvokeModel"]
}

variable "memory_actions" {
  description = "AgentCore Memory actions the execution role is granted, from `iam.memory_actions` in agent.yaml."
  type        = list(string)
  default = [
    "bedrock-agentcore:CreateEvent",
    "bedrock-agentcore:ListEvents",
    "bedrock-agentcore:RetrieveMemoryRecords",
  ]
}

variable "model_arns" {
  description = <<-EOT
    Resources for the Bedrock statement. Defaults to every foundation model and
    inference profile in the account's region, because a cross-region profile
    fans out to model ARNs in regions this module cannot know. Narrow it once the
    model is fixed.
  EOT
  type        = list(string)
  default     = []
}

variable "log_retention_days" {
  description = "CloudWatch retention for the runtime log group."
  type        = number
  default     = 30
}

variable "ecr_keep_last_images" {
  description = "Untagged images kept before the lifecycle policy expires them."
  type        = number
  default     = 10
}

variable "tags" {
  description = "Tags merged into every resource."
  type        = map(string)
  default     = {}
}
