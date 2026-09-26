# Runbook — sql_test_agent

Operational reference for running, building, deploying, invoking and debugging the agent.
The architecture and the reasoning behind it are in
[agent-platform-brief.md](agent-platform-brief.md); the payload contract is in
[`src/agents/sql_test_agent/README.md`](../src/agents/sql_test_agent/README.md).

Paths in this document are relative to the repository root.

---

## 1. Prerequisites

### Python 3.12

The repo pins `3.12` in [`.python-version`](../.python-version) and
[`pyproject.toml`](../pyproject.toml) declares `requires-python = ">=3.12"`. The container
base image is Python 3.12 as well. Match it locally; the code uses 3.10+ syntax
(`tuple[str, str]`, `X | None`) and there is no compatibility shim.

```bash
python3 --version        # expect 3.12.x
```

### AWS credentials

Standard boto3 resolution: `~/.aws/credentials`, environment variables, or an instance /
container role. The single check that matters:

```bash
aws sts get-caller-identity
```

If that fails, nothing below will work. Inside AgentCore Runtime the credentials come from
the runtime's **execution role**, which must trust `bedrock-agentcore.amazonaws.com`.

### Bedrock model access — this is per region

Model access in Amazon Bedrock is granted **per region**. Access granted in `eu-central-1`
does not apply in `us-east-1`. This is the most common deployment failure in this repo,
because `AWS_REGION` is set automatically inside Lambda and AgentCore Runtime — so the
agent silently calls Bedrock in whatever region it was deployed to, not the region you
tested in.

Grant it in the console for each region you deploy to:
**Bedrock → Model access → Modify model access**, then confirm from the shell:

```bash
aws bedrock list-foundation-models --region eu-central-1 \
  --query 'modelSummaries[].modelId' --output text | tr '\t' '\n' | grep -i sonnet
```

Cross-region model identifiers carry a geography prefix — `eu.` inside Europe, `us.`
elsewhere. A bare model id often fails with `on-demand throughput isn't supported`; the
prefixed id, or an inference-profile ARN, is what works.

### IAM

Two distinct principals, and confusing them is a classic time sink.

**The runtime execution role** (used by the container):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Effect": "Allow",
      "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
      "Resource": "*" },
    { "Effect": "Allow",
      "Action": ["ecr:GetAuthorizationToken", "ecr:BatchGetImage",
                 "ecr:GetDownloadUrlForLayer"],
      "Resource": "*" },
    { "Effect": "Allow",
      "Action": ["logs:CreateLogStream", "logs:PutLogEvents",
                 "logs:DescribeLogStreams"],
      "Resource": "*" }
  ]
}
```

Trust policy principal: `bedrock-agentcore.amazonaws.com`. Narrow
`bedrock:InvokeModel` to the specific model or inference-profile ARN once it works.

**The caller** (the `agent_invoker` Lambda's role, and anyone invoking it):

```json
{ "Effect": "Allow",
  "Action": "bedrock-agentcore:InvokeAgentRuntime",
  "Resource": "arn:aws:bedrock-agentcore:<REGION>:<ACCOUNT_ID>:runtime/<RUNTIME_ID>" }
```

### Terraform and Docker

Terraform for §4, and a Docker daemon able to produce `linux/arm64` images for §3.

---

## 2. Running the agent locally

### Set up the environment

```bash
python3 -m venv .venv
source .venv/bin/activate                       # Windows: .venv\Scripts\activate
pip install -r src/agents/sql_test_agent/requirements.txt
```

The agent's own `requirements.txt` — not the root one. The root
[`requirements.txt`](../requirements.txt) contains only `boto3>=1.43.0`, which is enough to
read the frozen reference but not enough to run the agent: the agent additionally needs the
AgentCore runtime SDK and the Strands framework. `src/agents/sql_test_agent/requirements.txt`
is the authoritative list.

### Configure

```bash
export BEDROCK_MODEL_ID=<model-id-or-inference-profile-arn>
export AWS_REGION=eu-central-1
# optional:
# export AGENT_MANIFEST=/path/to/a/scratch/agent.yaml    # try a manifest without editing it
# export MEMORY_ID=...                                   # memory is off while this is unset
```

You do **not** need to set `PYTHONPATH`. `sql_test_assistant.py` inserts
`src/agents/agents_base` into `sys.path` itself when that directory exists, which is what
lets the identical file run from a developer shell, from a test harness, and from the
container — where the Dockerfile has instead copied `tina_agent_base` next to the module.

`BEDROCK_MODEL_ID` has **no default**. Without it the server starts fine and every request
returns `status: "error"` — see §6.

### Start it

```bash
python src/agents/sql_test_agent/sql_test_assistant.py
```

`app.run()` starts the AgentCore HTTP server on port 8080. Exercise both endpoints of the
contract:

```bash
curl -s localhost:8080/ping

curl -s -X POST localhost:8080/invocations \
  -H 'content-type: application/json' \
  -d '{"sql": "SELECT order_id, amount FROM orders WHERE order_date IS NOT NULL", "context": "daily orders load"}' | python3 -m json.tool
```

That payload uses the `sql` key, which is one of the five accepted source fields. The same
request through the generic `prompt` field:

```bash
curl -s -X POST localhost:8080/invocations \
  -H 'content-type: application/json' \
  -d '{"prompt": "INSERT INTO finance.fct_ksb1 SELECT * FROM raw.ksb1", "context": "monthly close"}'
```

If `/ping` answers and `/invocations` returns cards, the runtime contract is satisfied and
the only things deployment adds are IAM, networking and the image.

---

## 3. Building the container

### The build

Build from the **repository root**, because the image needs both the agent directory and
the shared `src/agents/agents_base/` library:

```bash
docker build --platform linux/arm64 \
  -f src/agents/sql_test_agent/Dockerfile \
  -t sql_test_agent:local .
```

The Dockerfile's `COPY` paths are written from the repository root, and it copies three
things into `/app`: `tina_agent_base`, `sql_test_assistant.py`, and `agent.yaml` — the
manifest must sit beside the module because that is where `load_settings()` looks for it.
Building from inside the agent directory therefore fails: the shared library is outside the
context.

The image runs as a non-root user (uid 10001) and declares no `HEALTHCHECK` of its own,
because the runtime health-checks `/ping` itself.

### Run the image the way Runtime will

```bash
docker run --rm -p 8080:8080 \
  -e BEDROCK_MODEL_ID=<model-id-or-arn> \
  -e AWS_REGION=eu-central-1 \
  -v ~/.aws:/root/.aws:ro \
  sql_test_agent:local

curl -s localhost:8080/ping
curl -s -X POST localhost:8080/invocations -H 'content-type: application/json' \
  -d '{"sql": "SELECT order_id, amount FROM orders WHERE order_date IS NOT NULL", "context": "daily orders load"}'
```

Mounting `~/.aws` read-only stands in for the execution role. Do not bake credentials into
the image.

### Why arm64, specifically

**AgentCore Runtime only accepts `linux/arm64` images.** This is not a preference or an
optimisation; it is the service contract, alongside port 8080, `POST /invocations` and
`GET /ping`. An `amd64` image is a deployment error, not a slow deployment.

What makes it a trap is *where* it fails. `docker build` succeeds. `docker push` to ECR
succeeds — ECR stores any architecture. The failure appears only when the runtime is
created or updated and the image manifest is inspected, by which point the Terraform apply
is mid-flight and the error mentions the image, not your build command. Pin the platform
explicitly in the build command and in the Dockerfile's `FROM
--platform=linux/arm64 ...`, in both places, so neither a colleague's x86 laptop nor an
x86 CI runner can produce a wrong image.

Practically:

- **Apple Silicon** builds arm64 natively, fast, no extra setup.
- **x86 hosts** need emulation. Register the binfmt handlers and use a buildx builder:

  ```bash
  docker run --privileged --rm tonistiigi/binfmt --install arm64
  docker buildx create --use --name arm64 2>/dev/null || docker buildx use arm64
  docker buildx build --platform linux/arm64 \
    -f src/agents/sql_test_agent/Dockerfile \
    -t <ACCOUNT_ID>.dkr.ecr.eu-central-1.amazonaws.com/tmnl-tina-sql-test-agent:latest \
    --push .
  ```

  Emulated builds are slow — minutes, not seconds — which is normal, not a hang.

Verify before pushing, every time:

```bash
docker image inspect sql_test_agent:local --format '{{.Os}}/{{.Architecture}}'
# expect: linux/arm64
```

And after pushing:

```bash
aws ecr describe-images --repository-name tmnl-tina-sql-test-agent \
  --image-ids imageTag=<tag> --region eu-central-1 \
  --query 'imageDetails[0].imageManifestMediaType'
```

---

## 4. Deploying

Order matters: `tf_roots/shared/` owns the resources the agent roots consume — the
inference profile, the `agent_invoker` Lambda, the IAM and logging baseline — so it is
applied first.

### Step 1 — shared resources

```bash
terraform -chdir=tf_roots/shared init
terraform -chdir=tf_roots/shared plan
terraform -chdir=tf_roots/shared apply
```

### Step 2 — push the image

The agent root references an image tag in ECR. Build and push (§3) **before** applying, or
the runtime is created pointing at an image that does not exist.

```bash
aws ecr get-login-password --region eu-central-1 \
  | docker login --username AWS --password-stdin \
      <ACCOUNT_ID>.dkr.ecr.eu-central-1.amazonaws.com
docker push <ACCOUNT_ID>.dkr.ecr.eu-central-1.amazonaws.com/tmnl-tina-sql-test-agent:latest
```

The repository name and tag come from the `container:` block of
`src/agents/sql_test_agent/agent.yaml` (`tmnl-tina-sql-test-agent`, `latest`), which is the
same file Terraform reads.

### Step 3 — the agent root

```bash
terraform -chdir=tf_roots/sql_test_agent init
terraform -chdir=tf_roots/sql_test_agent plan
terraform -chdir=tf_roots/sql_test_agent apply
terraform -chdir=tf_roots/sql_test_agent output
```

Read the plan. This root manages exactly one runtime, so the plan is short enough to
review line by line — that is the reason for one root per agent.

Variables, backend and tfvars conventions live in the root itself; treat
`tf_roots/sql_test_agent/variables.tf` as authoritative over any list in this document.

### What a tuning change looks like

Every tuning value in `tina_agent_base.config.LIMITS`, plus `AGENT_NAME`, the model
identifier and the memory switches, resolves **environment → `agent.yaml` → tested
default**. So a tuning change is:

```
edit src/agents/sql_test_agent/agent.yaml  ->  terraform apply  ->  new runtime version
```

No image rebuild, no ECR push. The full list with defaults and minimums is in
[agent-platform-brief.md §10](agent-platform-brief.md#10-the-environment-variable-surface).
`agent.yaml` is copied into the image, so the same file also supplies the values for a local
`docker run` with no environment flags. Prompt overrides live in the manifest's `prompts:`
block and change the same way — with the caveat that an override missing a `{placeholder}`
is ignored with a warning rather than applied.

### Rollback

Re-apply the root with the previous image tag. The runtime keeps its ARN, so callers and the
Lambda need no change.

---

## 5. Invoking through the Lambda front door

Callers use the `agent_invoker` Lambda, not `invoke_agent_runtime` directly. The Lambda
resolves the session id and sends the source as the `prompt` field.

```bash
aws lambda invoke \
  --function-name <agent-invoker-function-name> \
  --cli-binary-format raw-in-base64-out \
  --payload '{
    "prompt": "INSERT INTO finance.fct_ksb1 SELECT c.* FROM raw.ksb1 c JOIN dim.cost_center d ON c.kostl = d.kostl",
    "context": "Monthly finance close. Reruns are common.",
    "runtimeSessionId": "finance-close-2026-09-fctksb1-run-000001"
  }' \
  --region eu-central-1 \
  out.json

python3 -m json.tool < out.json
```

Two things to check on every response:

```bash
python3 -c "
import json; r = json.load(open('out.json'))
print('status :', r.get('status'))
print('body   :', (r.get('test_cases_markdown') or r.get('error'))[:200])
"
```

- `status` is `"error"` inside an HTTP 200. A 200 does not mean the generation succeeded.
- A *successful* response may contain the out-of-scope sentinel rather than cards; see §6.

### Invoking the runtime directly, for debugging

Useful for taking the Lambda out of the picture:

```python
import boto3, json

client = boto3.client("bedrock-agentcore", region_name="eu-central-1")
response = client.invoke_agent_runtime(
    agentRuntimeArn="arn:aws:bedrock-agentcore:eu-central-1:<ACCOUNT_ID>:runtime/<RUNTIME_ID>",
    runtimeSessionId="debug-session-00000000000000000000001",
    payload=json.dumps({"sql": "SELECT 1", "context": ""}),
)
print(json.loads(response["response"].read()))
```

Requires `bedrock-agentcore:InvokeAgentRuntime` on your own principal.

### Timeout budgets must nest

The agent's Bedrock read timeout is **300 seconds**. Every timeout in front of it must be
larger, or the caller gives up while the model is still working and you get a client-side
timeout with no error in the agent's logs at all:

| Layer | Must be | Note |
|---|---|---|
| Bedrock read timeout | 300 s | `BEDROCK_READ_TIMEOUT`, a code constant |
| `agent_invoker` Lambda timeout | > 300 s | Lambda's ceiling is 900 s; the default 3 s is far too low |
| Any HTTP front end | > 300 s | API Gateway caps at 29 s and cannot front this synchronously |

---

## 6. Reading logs

### The two log lines that matter

The agent logs at `INFO` and brackets every successful request with two lines:

```
Generating test cases from 4213 characters of source (0 remembered turn(s))
Done: 1876 characters of test cases
```

They are the fastest triage tool in the repo:

- **Neither line** — the request never reached generation. A payload error (`read_payload`
  raised) or the container is not healthy.
- **First line only** — the request died during generation: a Bedrock error, a timeout, or a
  validation failure with nothing salvageable. A traceback from
  `logger.exception("Request failed")` follows.
- **Both lines** — the request succeeded. If the caller is still unhappy, the payload is
  fine and the content is the issue — check for a fallback answer or the out-of-scope
  sentinel.

Three `WARNING` lines are worth alerting on, because each one means the answer cost more or
delivered less than the happy path:

```
Validation failed, asking for one rewrite: TC-002 names 'orders', which does not ...
Returning 2 salvaged card(s) after validation failed: TC-003 is missing 'Pass criteria'.
Session memory disabled
```

The first means the request paid for two model calls. The second means the caller received a
**partial** answer — `fallback_used: true` in the response. The third is emitted once per
container and is only a problem if you expected memory to be on; it means `MEMORY_ID` is
empty.

Note what is *not* logged: never the source, never the context, never the assembled prompt,
never the nonce. Character counts only. Do not add prompt logging to debug an injection
report; the untrusted blob does not belong in CloudWatch.

`botocore`'s logger is pinned to `ERROR`, so AWS SDK chatter does not bury these lines.

### Where to look

AgentCore Runtime with observability enabled writes to a log group following the pattern
`/aws/bedrock-agentcore/runtimes/<runtime-id>-<endpoint>`. Confirm the actual name from the
Terraform output or the console rather than assuming it.

```bash
# the agent
aws logs tail /aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT \
  --region eu-central-1 --since 30m --follow

# the front door
aws logs tail /aws/lambda/<agent-invoker-function-name> \
  --region eu-central-1 --since 30m --follow

# every failure in the last day
aws logs filter-log-events \
  --log-group-name /aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT \
  --region eu-central-1 --start-time $(( ($(date +%s) - 86400) * 1000 )) \
  --filter-pattern '"Request failed"'
```

Correlate a caller's complaint to a run through the session id: it is attached to the Strands
agent as the `session.id` trace attribute, so it appears in traces. Note that it is echoed in
the response **only when the caller supplied it** — a generated id is an internal correlation
key and is deliberately not handed back. If a caller cannot tell you which run was theirs,
have them start supplying their own id.

---

## 7. Troubleshooting

Every row below is a failure mode reachable from the code as it stands, with the message as
it actually appears.

| Symptom, as it appears | Cause | Fix |
|---|---|---|
| `{"status":"error","error":"No Bedrock model configured. Set BEDROCK_MODEL_ID or SONNET5_INFERENCE_PROFILE_ARN."}` — on every request, while `/ping` is healthy | Both `BEDROCK_MODEL_ID` and `SONNET5_INFERENCE_PROFILE_ARN` are empty. Resolved at import, checked in `generate()`, so the container starts normally. | Set one of them in `agent.yaml` and `terraform apply`. Locally, `export BEDROCK_MODEL_ID=...`. `BEDROCK_MODEL_ID` wins when both are set. |
| Container never becomes healthy; startup log ends with `ValueError: MAX_TOKENS must be at least 1000, got 500.` (or `must be a whole number, got 'eight thousand'`) | `_env_int` validates every entry in `tina_agent_base.config.LIMITS` **at import** and raises. Deliberate: a silently clamped limit is worse than a visible failure. | Correct the value in `agent.yaml` or the Terraform environment block and re-apply. Minimums are listed in [agent-platform-brief.md §10](agent-platform-brief.md#limits--the-limits-registry). |
| Container never becomes healthy; `ValueError: MAX_CARD_WORDS (120) must exceed CARD_WORD_TARGET (120); otherwise a card written to spec is rejected.` | `load_settings` cross-checks the two word limits. Equal values mean the validator rejects a card written exactly to the prompt's spec. | Raise `MAX_CARD_WORDS` or lower `CARD_WORD_TARGET`. The tested pair is 200 / 120. |
| Container never becomes healthy; `ValueError: MEMORY_ENABLED must be a boolean, got 'maybe'.` | `env_flag` refuses to guess. A switch that silently reads as off is how a feature gets deployed disabled unnoticed. | Use one of `1/true/yes/on/enabled` or `0/false/no/off/disabled`. |
| Agent uses built-in prompts although `agent.yaml` declares overrides; log shows `prompts.test_case is missing {source}; using the built-in prompt.` | `Manifest.prompt` validates that an override still contains every placeholder the code interpolates, and falls back rather than failing a request with a `KeyError`. | Restore the missing `{placeholder}` in the manifest and re-apply. |
| `{"status":"error","error":"Model output was cut off at 8000 tokens. The source may be too large for one request; split it into smaller logical units."}` | `MaxTokensReachedException` from Strands, translated in `generate()`. The answer hit the `MAX_TOKENS` ceiling mid-card. Usually a large source producing long cards, sometimes a repair round rewriting all five cards at once. | Split the source into logical units (one job, one target table) and invoke per unit. If cards are genuinely being truncated at a reasonable length, raise `MAX_TOKENS` in `agent.yaml` (minimum 1000) — but treat that as the second option; the budget is already headroom over what the prompt asks for. |
| `{"status":"error","error":"Generated test cases failed validation: TC-001 is missing 'What to test'. TC-002 names 'orders', which does not appear in the source."}` | Validation failed, the single repair round (`MAX_VALIDATION_RETRIES = 1`) also failed. The message is the validator's own error list, so it names the card and the rule. | Read the individual errors — see the expanded guide below. Raising `MAX_VALIDATION_RETRIES` above `1` mostly buys latency and token spend, not correctness; the repair prompt already carries the exact defect list. In `sql_test_assistant.py` this point emits the fallback answer instead of erroring, so a hard error here means nothing at all validated. |
| `{"status":"error","error":"Source is 183492 characters; the maximum is 150000. Split it into smaller logical units."}` | `read_payload` rejected the source before any model call. Free and fast — no Bedrock cost. | Split the source. Raise `MAX_SOURCE_LENGTH` in `agent.yaml` only if you also mean to pay the token cost: 150000 characters is already about 40k tokens of input. |
| `{"status":"error","error":"Context is 61200 characters; the maximum is 50000."}` | `context` exceeded `MAX_CONTEXT_LENGTH`. Also pre-model, so free. | Trim the context to what actually constrains the tests. `MAX_CONTEXT_LENGTH` is tunable (`limits.max_context_length`, minimum 1000), but a 50k-character context is usually a sign the caller is pasting a second source rather than describing intent. |
| `{"status":"error","error":"...ThrottlingException... Too many requests, please wait before trying again."}`, intermittent, worse under concurrency | Bedrock throttling. botocore retries are deliberately disabled (`max_attempts: 1`) because Strands retries throttling itself; once Strands gives up, the exception surfaces. | Back off and retry at the caller with jitter. Point `SONNET5_INFERENCE_PROFILE_ARN` at an inference profile with more headroom, request a quota increase, or reduce caller concurrency. Do **not** re-enable botocore retries — that stacks two retry layers on a throttled endpoint. |
| `{"status":"error","error":"...AccessDeniedException... You don't have access to the model with the specified model ID."}` — works locally, fails deployed | Model access is granted **per region**. `AWS_REGION` is set automatically inside Runtime and Lambda, so the deployed agent calls Bedrock in its deployment region, not your test region. | Enable model access in the deployment region, or set `AWS_REGION` in `agent.yaml` to a region that has it, or use an inference-profile ARN resolving to an enabled region. Check with `aws bedrock list-foundation-models --region <deployment-region>`. |
| `{"status":"error","error":"...AccessDeniedException... not authorized to perform: bedrock:InvokeModel"}` | IAM, not model access. The execution role lacks the action, or a resource-scoped policy does not cover this model/profile ARN. | Add `bedrock:InvokeModel` (and `bedrock:InvokeModelWithResponseStream`) to the execution role, scoped to the model or profile ARN. |
| `{"status":"success","test_cases_markdown":"...","fallback_used":true,"validation_errors":["TC-003 is missing 'Pass criteria'."]}` | **Not a failure, but a partial answer.** Validation failed after the repair round, so fallback generation returned only the cards that individually validated. The log carries `Returning N salvaged card(s)`. | Nothing to fix operationally — the cards returned are valid. Downstream reports should surface `fallback_used` rather than silently treating a short answer as a complete one. A high rate of fallbacks on one source family is a prompt or source-shape problem worth a ticket. |
| `{"status":"success","test_cases_markdown":"OUT_OF_SCOPE: this input is not a recognizable SQL query, AWS Glue ETL job, or Airflow DAG."}` | **Not a failure.** The model judged the input out of scope and returned the sentinel line; `_validate_cards` accepts it and the request succeeds. | Check what was actually sent — usually a truncated file, a README, a YAML config, or the wrong payload field. Callers must branch on this sentinel, or they will store the line as a test plan. |
| `{"status":"error","error":"Missing source content. Supply one of: source, sql, glue_job, airflow_job, prompt."}` | None of the five `SOURCE_FIELDS` held a non-empty string. Whitespace-only counts as empty; a non-string value is ignored entirely. | Send the source in one of the five fields. Note `prompt` is last in precedence: if both `sql` and `prompt` are present, `sql` wins. |
| `{"status":"error","error":"Field 'context' must be a string"}` | `context` was present and not a string — a number, a list, a dict. Only `None` is defaulted to `""`. | Send a string, or omit the key. |
| `{"status":"error","error":"Payload must be a dictionary"}` | The payload deserialised to something other than an object — commonly a JSON string that was double-encoded. | Send a JSON object. Encode once. |
| `{"status":"error","error":"Model returned no text."}` | The model returned content blocks with no non-empty `text` block. Typically all output went to reasoning tokens, or the response was stopped before any text. | Confirm the model supports the call shape and that reasoning is not consuming the whole budget; raise `MAX_TOKENS`, or change model. |
| Caller times out, agent logs show `Generating test cases from N characters` and no `Done:` and no traceback | The caller's timeout is shorter than the agent's work. `BEDROCK_READ_TIMEOUT` is 300 s. | Raise the Lambda timeout above 300 s. API Gateway's 29 s ceiling means it cannot front this synchronously. |
| `Deploy fails when the runtime is created/updated, complaining about the image` | The image is not `linux/arm64`. ECR accepts any architecture, so `push` succeeded. | Rebuild with `--platform linux/arm64` and verify with `docker image inspect --format '{{.Os}}/{{.Architecture}}'` before pushing. See §3. |
| `ModuleNotFoundError: No module named 'tina_agent_base'` locally | `sql_test_assistant.py` adds `src/agents/agents_base` to `sys.path` only when that directory exists relative to the module. You are running a copy of the file from somewhere else. | Run it from its place in the checkout, or set `PYTHONPATH=src/agents/agents_base`. |
| `ModuleNotFoundError: No module named 'bedrock_agentcore'` / `'strands'` | The root `requirements.txt` has only `boto3`. | `pip install -r src/agents/sql_test_agent/requirements.txt`. |
| Local run logs `Could not read .../agent.yaml without PyYAML (...); using built-in defaults.` | `tina_agent_base.config` has a small standard-library YAML reader so that `config` stays importable with no dependencies, and it met syntax it does not implement. The container image pins a real YAML parser, so this is a local-only condition. | Harmless — the agent runs on the in-code defaults. Install the YAML parser from the agent's `requirements.txt` if you need the manifest honoured locally. |
| A limit set in `agent.yaml` appears to be ignored | The environment wins over the manifest. Terraform injects the variable, so an env value shadows the YAML. | Check the runtime's environment block for the variable named in [§10 of the brief](agent-platform-brief.md#limits--the-limits-registry); remove it there if the manifest should own the value. |
| `Signature expired` / `InvalidSignatureException` | Local clock skew. | `sudo sntp -sS time.apple.com` (macOS), or fix NTP. |
| `on-demand throughput isn't supported` | A bare foundation-model id was used where a cross-region profile is required. | Use the geography-prefixed id (`eu.` / `us.`) or an inference-profile ARN. |

### Reading a validation failure

The error string is the validator's own list, joined by spaces, so each fragment maps to a
specific rule. What to do differs per fragment:

| Fragment | What it means | What to do |
|---|---|---|
| `TC-001 is missing 'What to test'.` | For `What to test`, `Pass criteria` and `Failure means`, the parser needs the heading **alone on its line** with the body **indented**. A flush-left body, or `What to test : prose` on one line, parses as empty. | Nothing operational — this is the model flattening the layout, and it is the single most common reason the repair round is entered. If it fails persistently on a particular source, that source is worth attaching to a bug report. |
| `TC-001 names a temporary object in 'Source table'.` | The card named a `CREATE TEMP TABLE`, a `#temp`, or a CTE. Cards may only name objects that outlive the run. | Expected model behaviour on a source whose logic lives in temp steps; the repair round usually fixes it. If it never recovers, the persistent target may genuinely be hard to identify from the source — supply it in `context`. |
| `TC-001 names 'orders', which does not appear in the source.` | The occurrence check is exact, including schema qualification: if the source says `raw.orders`, a card saying `orders` is rejected, and vice versa. | Usually the model dropping or inventing a schema prefix. Also fires when a card writes `raw.orders (fact feed)` — the field takes names only, no annotations. |
| `The answer contains 7 cards; maximum is 5.` | More cards than `MAX_CARDS`. | Raise `MAX_CARDS` in `agent.yaml` if you genuinely want more; it feeds both the prompt and the check. |
| `TC-001 is longer than 200 words.` | `MAX_CARD_WORDS`, counted over the whole card including labels. The prompt asks for 120 (`CARD_WORD_TARGET`), so there is deliberate slack. | Both are tunable, but `MAX_CARD_WORDS` must stay above `CARD_WORD_TARGET` or the agent refuses to start. Persistent verbosity on one source is a prompt issue, not an ops issue. |
| `Duplicate card id TC-001.` | Two cards parsed to the same integer id. `TC-001` and `TC-1` are the **same** id. | Model error; the repair round normally fixes it. |
| `No test cards were found.` | Nothing matched `TC-<nnn>`. Often the model wrote prose, or wrote the out-of-scope sentinel with extra words appended — the sentinel match is exact. | Check whether the input is in scope at all. |

### Fast triage checklist

1. `status` in the body, not the HTTP code.
2. `aws logs tail` — are both log lines there, one, or neither? (§6)
3. Is `/ping` healthy? If not, suspect `_env_int` raising at import.
4. Is `AWS_REGION` in the runtime the region where model access was granted?
5. Is `BEDROCK_MODEL_ID` actually set in the runtime's environment block?
6. Reproduce locally with the same payload (§2) — that removes IAM, networking and the image
   from the picture in one step.
