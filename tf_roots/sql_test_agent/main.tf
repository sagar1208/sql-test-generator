# The deployable root for the sql_test_agent.
#
# Three things are wired together here and nothing else is decided:
#
#   * tf_modules/agent            -- the runtime, its image repository, its role,
#                                    its log group and its memory store;
#   * the shared inference profile -- read from tf_roots/shared, or passed in;
#   * src/lambdas/agent_invoker    -- the front door callers actually reach.
#
# Sizing, limits, environment variables and IAM actions are NOT declared here.
# They are read from the agent's own agent.yaml, so the manifest the agent loads
# at runtime and the manifest terraform deploys are the same file.

locals {
  agent_dir     = "${path.module}/../../src/agents/sql_test_agent"
  manifest_path = "${local.agent_dir}/agent.yaml"
  manifest      = yamldecode(file(local.manifest_path))

  agent_name = local.manifest.name

  # The manifest's tag is the default; var.image_tag overrides it for a
  # promotion, where the tag has to be pinned rather than followed.
  image_tag = var.image_tag != "" ? var.image_tag : local.manifest.container.image_tag

  default_tags = merge(
    {
      Platform    = "tmnl-tina-pipeline-bi-agents"
      Environment = var.environment
      CostCenter  = var.cost_center
      ManagedBy   = "terraform"
      Root        = "sql_test_agent"
    },
    var.tags,
  )

  # Either source is fine; having neither is not, and the module's precondition
  # is what says so rather than a confusing Bedrock error at the first request.
  inference_profile_arn = (
    var.inference_profile_arn != ""
    ? var.inference_profile_arn
    : try(data.terraform_remote_state.shared[0].outputs.inference_profile_arn, "")
  )

  lambda_source_dir = "${path.module}/../../src/lambdas/agent_invoker"
  lambda_name       = "tmnl-tina-${replace(local.agent_name, "_", "-")}-invoker-${var.environment}"

  # The Lambda must outlive the Bedrock call it is waiting on, and its own client
  # must give up first so the caller gets a 504 from us rather than a truncated
  # connection from AWS.
  request_seconds = local.manifest.timeouts.request_seconds
  lambda_timeout  = min(local.request_seconds, 900)
}

data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

data "terraform_remote_state" "shared" {
  count = var.shared_state == null ? 0 : 1

  backend = "s3"

  config = {
    bucket = var.shared_state.bucket
    key    = var.shared_state.key
    region = var.shared_state.region
  }
}

# --- the agent runtime ---------------------------------------------------------------

module "agent" {
  source = "../../tf_modules/agent"

  name        = local.agent_name
  description = local.manifest.description
  platform    = local.manifest.platform

  ecr_repository_name = local.manifest.container.ecr_repository
  image_tag           = local.image_tag

  # Straight from the manifest: one source of truth for what the container sees.
  environment = local.manifest.environment

  resources = {
    cpu                   = local.manifest.resources.cpu
    memory_mib            = local.manifest.resources.memory_mib
    ephemeral_storage_mib = local.manifest.resources.ephemeral_storage_mib
  }

  timeouts = {
    request_seconds      = local.manifest.timeouts.request_seconds
    session_idle_seconds = local.manifest.timeouts.session_idle_seconds
  }

  memory = {
    enabled           = local.manifest.memory.enabled
    name              = local.manifest.memory.name
    event_expiry_days = local.manifest.memory.event_expiry_days
    strategies        = local.manifest.memory.strategies
  }

  network_mode          = local.manifest.runtime.network_mode
  server_protocol       = local.manifest.runtime.server_protocol
  observability_enabled = local.manifest.runtime.observability

  bedrock_actions = local.manifest.iam.bedrock_actions
  memory_actions  = local.manifest.iam.memory_actions

  inference_profile_arn = local.inference_profile_arn

  log_retention_days = var.log_retention_days
  tags               = local.default_tags
}

# --- the invoker Lambda --------------------------------------------------------------

data "archive_file" "invoker" {
  type        = "zip"
  source_dir  = local.lambda_source_dir
  output_path = "${path.module}/.terraform/agent_invoker.zip"

  # __pycache__ from a local test run would change the hash on every apply.
  excludes = ["__pycache__", "*.pyc"]
}

data "aws_iam_policy_document" "invoker_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "invoker" {
  name               = "${local.lambda_name}-role"
  description        = "Execution role for the ${local.agent_name} invoker Lambda."
  assume_role_policy = data.aws_iam_policy_document.invoker_assume.json
  tags               = local.default_tags
}

data "aws_iam_policy_document" "invoker" {
  statement {
    sid    = "InvokeAgentRuntime"
    effect = "Allow"
    actions = [
      "bedrock-agentcore:InvokeAgentRuntime",
    ]
    # Both ARN shapes: the runtime, and the qualified endpoint a version or an
    # alias is invoked through.
    resources = [
      module.agent.runtime_arn,
      "${module.agent.runtime_arn}/*",
    ]
  }

  statement {
    sid    = "WriteOwnLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.invoker.arn}:*"]
  }
}

resource "aws_iam_role_policy" "invoker" {
  name   = "${local.lambda_name}-policy"
  role   = aws_iam_role.invoker.id
  policy = data.aws_iam_policy_document.invoker.json
}

resource "aws_cloudwatch_log_group" "invoker" {
  name              = "/aws/lambda/${local.lambda_name}"
  retention_in_days = var.log_retention_days
  tags              = local.default_tags
}

resource "aws_lambda_function" "invoker" {
  function_name = local.lambda_name
  description   = "Front door for the ${local.agent_name} AgentCore runtime."
  role          = aws_iam_role.invoker.arn

  filename         = data.archive_file.invoker.output_path
  source_code_hash = data.archive_file.invoker.output_base64sha256

  handler = "handler.handler"
  runtime = "python3.12"
  # Same architecture as the agent image: nothing here is native, and arm64 is
  # cheaper per millisecond of waiting.
  architectures = ["arm64"]

  timeout     = local.lambda_timeout
  memory_size = var.lambda_memory_mib

  environment {
    variables = {
      AGENT_RUNTIME_ARN       = module.agent.runtime_arn
      AGENT_RUNTIME_QUALIFIER = "DEFAULT"
      MAX_SOURCE_LENGTH       = local.manifest.environment.MAX_SOURCE_LENGTH
      # Give up before Lambda does, so the caller gets our 504 and not a
      # connection cut mid-answer.
      INVOKE_READ_TIMEOUT = local.lambda_timeout - 30
      SESSION_PREFIX      = "tina-${replace(local.agent_name, "_", "-")}"
      LOG_LEVEL           = local.manifest.environment.LOG_LEVEL
    }
  }

  tags = local.default_tags

  depends_on = [
    aws_iam_role_policy.invoker,
    aws_cloudwatch_log_group.invoker,
  ]
}

resource "aws_lambda_function_url" "invoker" {
  count = var.create_function_url ? 1 : 0

  function_name = aws_lambda_function.invoker.function_name
  # IAM, never NONE: the payload is source code and the answer is a test plan.
  authorization_type = "AWS_IAM"
}
