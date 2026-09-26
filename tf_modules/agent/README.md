# tf_modules/agent

One AgentCore agent runtime, and everything it needs to be invoked.

Every agent on this platform is deployed through this module, so a permission, a
timeout or a log retention decision is made once instead of once per agent root.

## What it creates

| Resource | Why |
|---|---|
| `aws_ecr_repository` + lifecycle policy | The ARM64 image AgentCore pulls. Untagged images expire; tagged ones stay so a rollback has something to roll back to. |
| `aws_iam_role` (execution) | Trusted by `bedrock-agentcore.amazonaws.com`, with `aws:SourceAccount` and `aws:SourceArn` conditions so it cannot be assumed on another account's behalf. |
| `aws_iam_role_policy` | `bedrock:InvokeModel`, image pull, log writes, workload-identity tokens, and — only when a store exists — the AgentCore Memory actions. |
| `aws_cloudwatch_log_group` | `/aws/bedrock-agentcore/runtimes/<runtime_name>`, created here so retention is managed rather than defaulted to "forever". |
| `awscc_bedrockagentcore_memory` | Short-term session memory, when `memory.enabled` is true. |
| `awscc_bedrockagentcore_runtime` | The agent runtime itself. |

## Usage

The calling root reads the agent's manifest and passes it through, so the
manifest stays the single source of truth for sizing, limits and environment:

```hcl
locals {
  manifest = yamldecode(file("${path.module}/../../src/agents/sql_test_agent/agent.yaml"))
}

module "agent" {
  source = "../../tf_modules/agent"

  name                = local.manifest.name
  description         = local.manifest.description
  platform            = local.manifest.platform
  ecr_repository_name = local.manifest.container.ecr_repository
  image_tag           = local.manifest.container.image_tag

  environment = local.manifest.environment
  resources = {
    cpu        = local.manifest.resources.cpu
    memory_mib = local.manifest.resources.memory_mib
  }
  timeouts = {
    request_seconds      = local.manifest.timeouts.request_seconds
    session_idle_seconds = local.manifest.timeouts.session_idle_seconds
  }
  memory = {
    enabled           = local.manifest.memory.enabled
    name              = local.manifest.memory.name
    event_expiry_days = local.manifest.memory.event_expiry_days
  }

  bedrock_actions = local.manifest.iam.bedrock_actions
  memory_actions  = local.manifest.iam.memory_actions

  inference_profile_arn = module.inference_profile.arn
}
```

## Things worth knowing before you apply

**The image must exist first.** `CreateAgentRuntime` validates the container URI,
so a first apply into an empty repository fails. Push the image, then apply — or
apply with `-target=module.agent.aws_ecr_repository.this`, push, then apply in
full.

**Names are not interchangeable.** AgentCore runtime names accept letters, digits
and underscores only, so the module derives `tmnl_tina_<agent>` for the runtime
and `tmnl-tina-<agent>` for everything else. That is why `var.name` is validated
against a lowercase-underscore pattern.

**`resources` is recorded, not enforced.** AgentCore Runtime is serverless and
`CreateAgentRuntime` takes no CPU or memory arguments. The declared sizing is
written into tags (`DeclaredCpu`, `DeclaredMemoryMib`) so cost reporting and the
Fargate fallback path read the same numbers the manifest declares, rather than
two sets that drift.

**Empty environment values are dropped.** The manifest ships
`BEDROCK_MODEL_ID: ""` as a placeholder. Sending that through would give the
container a model id of `""`, which fails every request with a confusing Bedrock
error; dropping it instead lets the agent report "no Bedrock model configured"
and name the fix. The same applies to `MEMORY_ID`: absent means memoryless,
which is a supported mode.

**Exactly one model source.** A `lifecycle` precondition fails the plan when
neither `model_id` nor `inference_profile_arn` is set, because the agent has no
default model and would otherwise deploy cleanly and fail every call.

## Provider choice

AgentCore Runtime and Memory are managed through the `awscc` provider
(Cloud Control API), which is generated from the CloudFormation schema and
therefore tracks new AgentCore properties as they ship. If your `aws` provider
version already carries native `aws_bedrockagentcore_runtime` and
`aws_bedrockagentcore_memory` resources and you would rather use them, the two
resources in `main.tf` are the only places to change; the attribute names map
one-to-one onto the CloudFormation properties (`agent_runtime_name`,
`agent_runtime_artifact.container_configuration.container_uri`, `role_arn`,
`network_configuration.network_mode`, `protocol_configuration.server_protocol`,
`environment_variables`).

Pin the provider version you verified against — Cloud Control resources gain
attributes between releases, and an unpinned upgrade can turn an optional
attribute into a diff.

## Inputs

See `variables.tf`; each carries its own description. The ones most often set:
`name`, `ecr_repository_name`, `image_tag`, `environment`, `memory`,
`inference_profile_arn` or `model_id`.

## Outputs

`runtime_arn` (what the invoker Lambda calls), `runtime_id`, `runtime_version`,
`execution_role_arn`, `execution_role_name`, `ecr_repository_url`, `image_uri`,
`log_group_name`, `memory_id`, `memory_arn`, and `environment` — the variables
actually sent to the container, which is the quickest way to see what the
manifest resolved to.
