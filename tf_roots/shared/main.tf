# Shared resources: everything an agent root consumes but does not own.
#
# Applied once per account and region, before any agent root. Agent roots read
# its outputs through terraform_remote_state, so an agent never creates a model
# reference or a policy of its own and the account keeps one answer to "which
# model are we on".

locals {
  default_tags = merge(
    {
      Platform    = "tmnl-tina-pipeline-bi-agents"
      Environment = var.environment
      CostCenter  = var.cost_center
      ManagedBy   = "terraform"
      Root        = "shared"
    },
    var.tags,
  )
}

data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

# --- the model the agents call -------------------------------------------------------

module "inference_profile" {
  source = "../../tf_modules/inference_profile"

  name                   = "${var.name_prefix}-sonnet5-${var.environment}"
  description            = "Sonnet for the TINA pipeline BI agents (${var.environment})."
  model_id               = var.sonnet_model_id
  system_defined_profile = true

  # Bedrock attributes token usage to this profile, so these tags are what make
  # per-agent model spend reportable.
  tags = local.default_tags
}

# --- shared IAM ----------------------------------------------------------------------

# A managed policy rather than an inline one in each agent root: the model ARNs a
# cross-region profile fans out to change when the model does, and one attachment
# point means one apply to fix them everywhere.
data "aws_iam_policy_document" "invoke_shared_model" {
  statement {
    sid    = "InvokeSharedInferenceProfile"
    effect = "Allow"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    resources = concat(
      [module.inference_profile.arn],
      module.inference_profile.models,
    )
  }

  statement {
    sid       = "ReadProfileMetadata"
    effect    = "Allow"
    actions   = ["bedrock:GetInferenceProfile"]
    resources = [module.inference_profile.arn]
  }
}

resource "aws_iam_policy" "invoke_shared_model" {
  name        = "${var.name_prefix}-invoke-shared-model-${var.environment}"
  description = "Invoke the shared TINA inference profile and the models it routes to."
  policy      = data.aws_iam_policy_document.invoke_shared_model.json
  tags        = local.default_tags
}

# --- remote state scaffolding --------------------------------------------------------

# The bucket and table an agent root's backend points at. Created here, and used
# by this root only after the bootstrap apply described in versions.tf.
resource "aws_s3_bucket" "state" {
  bucket = "${var.name_prefix}-tfstate-${data.aws_caller_identity.current.account_id}"

  # State is the one thing that must survive a `terraform destroy` of everything
  # else, so removing it is a deliberate two-step.
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id

  versioning_configuration {
    # A corrupted apply is recovered by rolling back to the previous object
    # version. Without this there is nothing to roll back to.
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
