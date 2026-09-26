# tf_roots/sql_test_agent

Deploys the SQL test runtime: the agent, the model it calls, and the front door
callers reach it through.

```
caller ──▶ agent_invoker Lambda ──▶ AgentCore runtime ──▶ Bedrock (shared profile)
                                          │
                                          └──▶ AgentCore Memory (session turns)
```

## What this root decides, and what it does not

It wires three things together:

| Piece | Source |
|---|---|
| Agent runtime, ECR repo, execution role, log group, memory store | `tf_modules/agent` |
| Inference profile | `tf_roots/shared`, via remote state or `var.inference_profile_arn` |
| Invoker Lambda | `src/lambdas/agent_invoker`, zipped from the checkout |

It decides **none** of the agent's configuration. Sizing, timeouts, limits,
environment variables and IAM actions are read out of
`src/agents/sql_test_agent/agent.yaml` with `yamldecode`, so the manifest the
container loads at runtime and the manifest terraform deploys are one file. A
retune is an edit there plus an apply — no image rebuild, no variable to keep in
sync in two places.

## First deploy

The order matters: `CreateAgentRuntime` validates the container URI, so the image
has to exist before the runtime can be created.

```bash
# 1. shared resources first (see ../shared/README.md)
cd ../shared && terraform apply

# 2. the repository, on its own
cd ../sql_test_agent
cp terraform.tfvars.example terraform.tfvars   # then edit
terraform init
terraform apply -target=module.agent.aws_ecr_repository.this

# 3. build and push the ARM64 image FROM THE REPOSITORY ROOT
REPO=$(terraform output -raw ecr_repository_url)
aws ecr get-login-password --region eu-central-1 \
  | docker login --username AWS --password-stdin "${REPO%%/*}"
cd ../..
docker build --platform linux/arm64 \
  -f src/agents/sql_test_agent/Dockerfile -t "$REPO:latest" .
docker push "$REPO:latest"

# 4. the rest
cd tf_roots/sql_test_agent && terraform apply
```

Later deploys are just steps 3 and 4.

## Smoke test

```bash
aws lambda invoke \
  --function-name "$(terraform output -raw invoker_function_name)" \
  --payload "$(jq -Rs '{prompt: .}' < ../../src/agents/sql_test_agent/tmnl-tina-glue-job-if-finance-fctksb1.py | base64)" \
  --cli-binary-format base64 out.json
jq -r '.body | fromjson | .test_cases_markdown' out.json
```

The Glue job shipped beside the agent is the fixture for this: it contains a temp
view, a CTE, a `#temp` staging table, a fan-out `LEFT JOIN`, a truncating cast and
an off-by-one rolling window, so a good answer names `finance_dwh.fct_ksb1` and
never `stg_ksb1_lines`.

## Variables worth setting

| Variable | Why |
|---|---|
| `image_tag` | `latest` is a dev convenience. AgentCore resolves the tag **once**, at create time, so a promotion must pin a release tag or a digest. |
| `shared_state` | Normal wiring: reads `inference_profile_arn` from the shared root. |
| `inference_profile_arn` | Escape hatch: apply against an existing profile with no remote state at all. |
| `create_function_url` | IAM-authenticated URL for a smoke test. Real clients belong behind an API Gateway. |

## Failure modes this root has already hit

**`ValidationException` on the runtime.** The image is not arm64, or the tag does
not exist. `docker build --platform linux/arm64`, from the repository root so the
shared library is in the build context.

**Every request returns "No Bedrock model configured".** Neither
`inference_profile_arn` nor `shared_state` was set. The module's `lifecycle`
precondition fails the plan for this now, rather than letting it deploy cleanly
and fail at the first call.

**`AccessDeniedException` on the first invocation.** Bedrock model access is
granted per region, by hand, in the console. Terraform cannot grant it and will
not warn about it.

**Changing a limit did nothing.** Environment variables win over `agent.yaml`
inside the container, and this root injects the manifest's `environment` block as
environment variables. Change the value in `environment`, not only in `limits`.
`scripts/validate_manifests.py` fails when the two disagree, for exactly this
reason.

## Outputs

`agent_runtime_arn`, `agent_runtime_version`, `ecr_repository_url`, `image_uri`,
`agent_log_group`, `memory_id`, `agent_environment` (what the container actually
received), `invoker_function_name`, `invoker_function_arn`,
`invoker_log_group`, `invoker_function_url`, `inference_profile_arn`.
