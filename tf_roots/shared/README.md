# tf_roots/shared

Resources every agent root consumes but none of them owns. Applied once per
account and region, **before** any agent root.

## What lives here

| Resource | Why it is shared |
|---|---|
| Application inference profile (`tf_modules/inference_profile`) | One answer to "which model are we on". A model upgrade is one apply here, not one environment variable per agent. |
| `aws_iam_policy` — invoke the shared model | A cross-region profile fans out to model ARNs that change with the model. One attachment point means one apply to fix them everywhere. |
| S3 state bucket (versioned, encrypted, public access blocked, `prevent_destroy`) | The backend agent roots point at. |

## Applying it the first time

This root has a bootstrapping problem: the bucket that holds its state is
created *by* it. So:

```bash
terraform init                     # local state
terraform apply                    # creates the bucket and the profile
# uncomment the backend block in versions.tf, then:
terraform init -migrate-state
```

State is held with S3 native locking (`use_lockfile = true`, Terraform >= 1.10),
so there is no DynamoDB lock table to create or pay for.

## How agent roots consume it

```hcl
data "terraform_remote_state" "shared" {
  backend = "s3"
  config = {
    bucket = "tmnl-tina-tfstate-<account_id>"
    key    = "tmnl-tina-pipeline-bi-agents/shared/terraform.tfstate"
    region = "eu-central-1"
  }
}

# -> data.terraform_remote_state.shared.outputs.inference_profile_arn
```

`tf_roots/sql_test_agent` also accepts `inference_profile_arn` directly, so a
developer can apply an agent root against an existing profile without wiring
remote state at all.

## Outputs

| Output | Used by |
|---|---|
| `inference_profile_arn` | `SONNET5_INFERENCE_PROFILE_ARN` in every agent |
| `inference_profile_models` | An agent root narrowing its own Bedrock policy |
| `invoke_shared_model_policy_arn` | Attached to an agent execution role |
| `state_bucket` | The backend block in every other root |
| `region` | Sanity-checking that an agent root targets the same region |

## Worth knowing

**Bedrock model access is granted per region, by hand, in the console.** This
root creates the profile happily without it; the first `InvokeModel` is what
fails, with an `AccessDeniedException` that says nothing about model access.

**Destroying this root breaks every agent.** The profile ARN is baked into each
runtime's environment, so a destroy-and-recreate hands out a new ARN and every
agent has to be applied again. The state bucket carries `prevent_destroy` for the
same reason.
