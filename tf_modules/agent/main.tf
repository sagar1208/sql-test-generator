# One AgentCore agent runtime, with everything it needs to be invoked:
# an image repository, an execution role, a log group, an optional memory store,
# and the runtime itself.
#
# Every agent on this platform is deployed through this module, so a permission
# or a timeout is fixed in one place rather than in one root per agent.

terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.60"
    }
    # AgentCore Runtime and Memory are reached through Cloud Control, which
    # tracks the CloudFormation schema and therefore covers new AgentCore
    # properties as soon as they ship. See README.md for the swap to the native
    # aws_bedrockagentcore_* resources once your provider version has them.
    awscc = {
      source  = "hashicorp/awscc"
      version = ">= 1.20"
    }
  }
}

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

data "aws_partition" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.name
  partition  = data.aws_partition.current.partition

  # AgentCore runtime names take letters, digits and underscores only.
  runtime_name = replace("${var.name_prefix}_${var.name}", "-", "_")
  # Log groups, roles and repositories read better with hyphens.
  resource_name = replace("${var.name_prefix}-${var.name}", "_", "-")

  memory_enabled = coalesce(var.memory.enabled, false)
  memory_name = coalesce(
    var.memory.name != "" ? var.memory.name : null,
    "${local.runtime_name}_sessions",
  )
  memory_id = local.memory_enabled ? awscc_bedrockagentcore_memory.this[0].memory_id : ""

  container_uri = "${aws_ecr_repository.this.repository_url}:${var.image_tag}"

  log_group_name = "/aws/bedrock-agentcore/runtimes/${local.runtime_name}"

  # Resolved by terraform, so the manifest's placeholders never reach the
  # container: an empty BEDROCK_MODEL_ID would fail every request.
  resolved_environment = {
    BEDROCK_MODEL_ID              = var.model_id
    SONNET5_INFERENCE_PROFILE_ARN = var.inference_profile_arn
    MEMORY_ID                     = local.memory_id
    MEMORY_ENABLED                = local.memory_enabled ? "true" : "false"
  }

  # Empty values are dropped rather than sent: the agent distinguishes "unset"
  # from "set to nothing", and an empty MEMORY_ID means memoryless, not broken.
  environment = {
    for key, value in merge(var.environment, local.resolved_environment) :
    key => tostring(value)
    if tostring(value) != ""
  }

  model_arns = length(var.model_arns) > 0 ? var.model_arns : [
    "arn:${local.partition}:bedrock:${local.region}::foundation-model/*",
    "arn:${local.partition}:bedrock:${local.region}:${local.account_id}:inference-profile/*",
    "arn:${local.partition}:bedrock:*:${local.account_id}:application-inference-profile/*",
  ]

  tags = merge(
    {
      Platform             = "tmnl-tina-pipeline-bi-agents"
      Agent                = var.name
      ManagedBy            = "terraform"
      DeclaredCpu          = tostring(var.resources.cpu)
      DeclaredMemoryMib    = tostring(var.resources.memory_mib)
      DeclaredRequestSecs  = tostring(coalesce(var.timeouts.request_seconds, 900))
      DeclaredIdleSessSecs = tostring(coalesce(var.timeouts.session_idle_seconds, 1800))
    },
    var.tags,
  )
}

# --- image repository ----------------------------------------------------------------

resource "aws_ecr_repository" "this" {
  name                 = var.ecr_repository_name
  image_tag_mutability = "MUTABLE"
  force_delete         = false

  image_scanning_configuration {
    # A base image is rebuilt on every deploy, so a finding here is actionable.
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }

  tags = local.tags
}

resource "aws_ecr_lifecycle_policy" "this" {
  repository = aws_ecr_repository.this.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images; a rollback uses a tagged one."
        selection = {
          tagStatus   = "untagged"
          countType   = "imageCountMoreThan"
          countNumber = var.ecr_keep_last_images
        }
        action = {
          type = "expire"
        }
      },
    ]
  })
}

# --- execution role ------------------------------------------------------------------

data "aws_iam_policy_document" "assume" {
  statement {
    sid     = "AgentCoreRuntimeAssume"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }

    # Confused-deputy guards: only this account's AgentCore, only these runtimes.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:${local.partition}:bedrock-agentcore:${local.region}:${local.account_id}:*"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name                 = "${local.resource_name}-execution"
  description          = "Execution role for the ${var.name} AgentCore runtime."
  assume_role_policy   = data.aws_iam_policy_document.assume.json
  max_session_duration = 3600
  tags                 = local.tags
}

data "aws_iam_policy_document" "execution" {
  statement {
    sid       = "InvokeBedrockModels"
    effect    = "Allow"
    actions   = var.bedrock_actions
    resources = local.model_arns
  }

  statement {
    sid    = "PullAgentImage"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = [aws_ecr_repository.this.arn]
  }

  statement {
    sid       = "EcrAuth"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid    = "WriteLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
    ]
    resources = [
      aws_cloudwatch_log_group.runtime.arn,
      "${aws_cloudwatch_log_group.runtime.arn}:log-stream:*",
    ]
  }

  # Session isolation means the runtime asks for a workload identity token per
  # session; without this every invocation fails before the agent starts.
  statement {
    sid    = "WorkloadIdentity"
    effect = "Allow"
    actions = [
      "bedrock-agentcore:GetWorkloadAccessToken",
      "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
      "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
    ]
    resources = [
      "arn:${local.partition}:bedrock-agentcore:${local.region}:${local.account_id}:workload-identity-directory/default",
      "arn:${local.partition}:bedrock-agentcore:${local.region}:${local.account_id}:workload-identity-directory/default/workload-identity/${local.runtime_name}-*",
    ]
  }

  dynamic "statement" {
    # Only granted when a store exists: an agent that cannot use memory should
    # not hold permissions on anyone else's.
    for_each = local.memory_enabled ? [1] : []

    content {
      sid       = "SessionMemory"
      effect    = "Allow"
      actions   = var.memory_actions
      resources = [
        awscc_bedrockagentcore_memory.this[0].memory_arn,
        "${awscc_bedrockagentcore_memory.this[0].memory_arn}/*",
      ]
    }
  }

  dynamic "statement" {
    for_each = var.observability_enabled ? [1] : []

    content {
      sid    = "EmitTraces"
      effect = "Allow"
      actions = [
        "xray:PutTraceSegments",
        "xray:PutTelemetryRecords",
        "cloudwatch:PutMetricData",
      ]
      resources = ["*"]
    }
  }
}

resource "aws_iam_role_policy" "execution" {
  name   = "${local.resource_name}-execution"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution.json
}

# --- logs --------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "runtime" {
  name              = local.log_group_name
  retention_in_days = var.log_retention_days
  tags              = local.tags
}

# --- session memory ----------------------------------------------------------------

resource "awscc_bedrockagentcore_memory" "this" {
  count = local.memory_enabled ? 1 : 0

  name                  = local.memory_name
  description           = "Short-term session memory for the ${var.name} agent."
  event_expiry_duration = coalesce(var.memory.event_expiry_days, 7)

  tags = local.tags
}

# --- the runtime -------------------------------------------------------------------

resource "awscc_bedrockagentcore_runtime" "this" {
  agent_runtime_name = local.runtime_name
  description        = var.description != "" ? var.description : "TINA ${var.name} agent."
  role_arn           = aws_iam_role.execution.arn

  agent_runtime_artifact = {
    container_configuration = {
      container_uri = local.container_uri
    }
  }

  network_configuration = {
    network_mode = var.network_mode
  }

  protocol_configuration = {
    server_protocol = var.server_protocol
  }

  environment_variables = local.environment

  tags = local.tags

  depends_on = [
    aws_iam_role_policy.execution,
    aws_cloudwatch_log_group.runtime,
  ]

  lifecycle {
    precondition {
      condition     = var.model_id != "" || var.inference_profile_arn != ""
      error_message = "Set model_id or inference_profile_arn. The agent has no default model and fails every request without one."
    }
  }
}
