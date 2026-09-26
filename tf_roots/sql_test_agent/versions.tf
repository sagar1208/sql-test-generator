terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.60"
    }
    # AgentCore Runtime and Memory, through Cloud Control. See
    # tf_modules/agent/README.md for why, and for the swap to the native
    # aws_bedrockagentcore_* resources.
    awscc = {
      source  = "hashicorp/awscc"
      version = ">= 1.20"
    }
    # Zips the invoker Lambda from the checkout, so the function is deployed
    # from the same commit as the agent it calls.
    archive = {
      source  = "hashicorp/archive"
      version = ">= 2.4"
    }
  }

  # Uncomment once tf_roots/shared has created the bucket.
  #
  # backend "s3" {
  #   bucket       = "tmnl-tina-tfstate-<account_id>"
  #   key          = "tmnl-tina-pipeline-bi-agents/sql_test_agent/terraform.tfstate"
  #   region       = "eu-central-1"
  #   encrypt      = true
  #   use_lockfile = true
  # }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = local.default_tags
  }
}

provider "awscc" {
  region = var.region
}
