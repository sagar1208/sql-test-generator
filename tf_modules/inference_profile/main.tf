# A Bedrock application inference profile wrapping one foundation model or one
# system-defined (cross-region) profile.
#
# Two reasons this exists rather than agents holding a bare model id:
#
#   * cost attribution -- Bedrock reports usage per application inference
#     profile, so tags on this resource are what separate one agent's model spend
#     from another's;
#   * indirection -- agents receive an ARN that does not change when the model
#     behind it does, so a model upgrade is one apply here instead of one
#     environment variable per agent.

terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.60"
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

  # A system-defined profile is account-scoped; a foundation model is not.
  derived_arn = var.system_defined_profile ? (
    "arn:${local.partition}:bedrock:${local.region}:${local.account_id}:inference-profile/${var.model_id}"
    ) : (
    "arn:${local.partition}:bedrock:${local.region}::foundation-model/${var.model_id}"
  )

  source_arn = var.model_source_arn != "" ? var.model_source_arn : local.derived_arn
}

resource "aws_bedrock_inference_profile" "this" {
  name        = var.name
  description = var.description != "" ? var.description : "Application inference profile for ${var.name}."

  model_source {
    copy_from = local.source_arn
  }

  tags = var.tags

  lifecycle {
    precondition {
      condition     = var.model_source_arn != "" || var.model_id != ""
      error_message = "Set model_id or model_source_arn. An inference profile has to wrap something."
    }
  }
}
