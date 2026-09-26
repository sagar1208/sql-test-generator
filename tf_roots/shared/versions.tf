terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.60"
    }
  }

  # Remote state, commented until the state bucket exists.
  #
  # This root is the first thing applied in a new account, so it has a
  # bootstrapping problem: the bucket that holds its state does not exist yet.
  # Apply once with local state, create the bucket and lock table (or turn on
  # S3 native locking, below), then uncomment this block and `terraform init
  # -migrate-state`.
  #
  # backend "s3" {
  #   bucket       = "tmnl-tina-tfstate-<account_id>"
  #   key          = "tmnl-tina-pipeline-bi-agents/shared/terraform.tfstate"
  #   region       = "eu-central-1"
  #   encrypt      = true
  #   use_lockfile = true # S3 native locking; no DynamoDB table needed (>= 1.10)
  # }
}

provider "aws" {
  region = var.region

  # Applied to everything in this root, so a resource cannot be created
  # untagged by forgetting to pass tags through a module.
  default_tags {
    tags = local.default_tags
  }
}
