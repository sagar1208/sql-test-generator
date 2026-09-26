# tf_modules/inference_profile

One Bedrock **application inference profile**, wrapping a foundation model or a
system-defined cross-region profile.

## Why agents do not hold a model id

**Cost attribution.** Bedrock reports token usage per application inference
profile. Tags on this resource are therefore the only way to say what an agent's
model spend was, rather than seeing one undifferentiated Bedrock line on the
bill.

**Indirection.** Agents receive `SONNET5_INFERENCE_PROFILE_ARN`, an ARN that does
not change when the model behind it does. A model upgrade is one apply here
instead of one environment variable per agent, per environment.

**Throughput.** Pointing the profile at a system-defined *cross-region* profile
(`eu.anthropic...`) lets Bedrock spread a burst across regions. That is what
stops a 150k-character source being throttled halfway through a run.

## Usage

```hcl
module "inference_profile" {
  source = "../../tf_modules/inference_profile"

  name                   = "tmnl-tina-sonnet5"
  description            = "Sonnet for the TINA BI agents."
  model_id               = "eu.anthropic.claude-sonnet-4-5-20250929-v1:0"
  system_defined_profile = true

  tags = {
    Platform    = "tmnl-tina-pipeline-bi-agents"
    CostCenter  = "bi-platform"
  }
}
```

Wrapping a plain foundation model instead:

```hcl
  model_id               = "anthropic.claude-sonnet-4-5-20250929-v1:0"
  system_defined_profile = false
```

## The ARN shapes, because they are easy to get wrong

| Source | ARN | Account-scoped |
|---|---|---|
| Foundation model | `arn:aws:bedrock:<region>::foundation-model/<model_id>` | no (note the empty account field) |
| System-defined profile | `arn:aws:bedrock:<region>:<account>:inference-profile/<profile_id>` | yes |
| Application profile (this module's output) | `arn:aws:bedrock:<region>:<account>:application-inference-profile/<id>` | yes |

`system_defined_profile` picks between the first two. It defaults to `true`
because a regional prefix (`eu.`, `us.`) is the usual and better choice. Set
`model_source_arn` directly when neither shape fits.

## Before you apply

**Model access is per region and must be granted first.** Creating the profile
succeeds without it; the first `InvokeModel` is what fails, with an
`AccessDeniedException` that does not mention model access.

**IAM needs the profile and the models behind it.** A cross-region profile fans
out to model ARNs in several regions, so a policy that allows only the profile
ARN fails at invocation time. The `models` output lists what it routes to, which
is what a narrowed policy should be built from.

## Outputs

`arn` (feeds `SONNET5_INFERENCE_PROFILE_ARN`), `id`, `name`,
`model_source_arn`, and `models`.
