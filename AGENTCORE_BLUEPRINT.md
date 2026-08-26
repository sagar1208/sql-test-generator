# Deploying to Amazon Bedrock AgentCore Runtime — Blueprint

How the current local CLI becomes a hosted agent, and what AgentCore actually does
for you.

---

## 1. What AgentCore is

Amazon Bedrock AgentCore is a set of **independent services** for running agents in
production. You adopt only the pieces you need.

| Service | What it does | Needed here? |
|---|---|---|
| **Runtime** | Serverless hosting for agent code. Session isolation, up to 8-hour sessions, scales to zero. | **Yes** — this is the deployment target |
| **Memory** | Short- and long-term conversational memory | No — the pipeline is stateless |
| **Identity** | OAuth / credential brokering for agents calling third-party APIs | No |
| **Gateway** | Turns existing APIs and Lambdas into MCP tools | No — no external tools |
| **Browser** | Managed headless browser as a tool | No |
| **Code Interpreter** | Sandboxed code execution as a tool | No |
| **Observability** | OTEL traces and CloudWatch dashboards for agent runs | Optional, cheap to enable |

**The key idea:** AgentCore Runtime is *framework-agnostic*. It does not care whether
you use LangGraph, CrewAI, Strands, or plain boto3 as this project does. It only cares
that your container answers HTTP on the agreed contract. There is no rewrite into an
"agent framework" required.

## 2. How Runtime works

```
Client
  │  InvokeAgentRuntime  (SigV4 or OAuth)
  ▼
AgentCore Runtime  ─── routes by sessionId ──▶  isolated microVM
                                                  │
                                                  ▼
                                          your container (ARM64)
                                          POST :8080/invocations
                                          GET  :8080/ping
                                                  │
                                                  ▼
                                          bedrock-runtime.converse
```

**The service contract** (verified against AWS docs):

- Container listens on **port 8080** for the HTTP protocol
- **`POST /invocations`** — receives the payload, returns JSON or an SSE stream
- **`GET /ping`** — health check, must report healthy
- Image must be **`linux/arm64`** and live in **ECR**
- Auth is **SigV4** or **OAuth 2.0**; callers need `bedrock-agentcore:InvokeAgentRuntime`

Runtime also supports MCP (port 8000), A2A (9000), and AG-UI protocols. This project
wants plain **HTTP**.

**Session isolation** is the feature that distinguishes Runtime from Lambda: each
`sessionId` gets a dedicated microVM that persists across calls for up to 8 hours,
so an agent can hold state in memory between turns. This pipeline is stateless, so it
gains nothing from that — see §6.

## 3. Required code changes

Small. `invoke(payload) -> dict` in [agent.py](agent.py) already matches the
entrypoint shape. The `bedrock-agentcore` SDK supplies the HTTP server, so you do not
hand-write `/invocations` or `/ping`.

**New file — `app.py`:**

```python
"""AgentCore Runtime entrypoint. Wraps the existing pipeline."""

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from agent import invoke as run_pipeline

app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload: dict) -> dict:
    return run_pipeline(payload)


if __name__ == "__main__":
    app.run()
```

`agent.py`, `config.py`, `agent.yaml`, and `llm_client.py` stay exactly as they are.
The CLI keeps working locally.

**New file — `Dockerfile`:**

```dockerfile
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt bedrock-agentcore

COPY agent.py config.py llm_client.py app.py agent.yaml ./

EXPOSE 8080
CMD ["python", "app.py"]
```

**`requirements.txt`** gains `bedrock-agentcore`. `boto3` and `PyYAML` stay.

`agent.yaml` must be copied into the image — the prompts live there.

## 4. IAM

Two distinct roles — a common source of confusion.

**Execution role** — assumed by Runtime, used by your container:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": ["ecr:GetAuthorizationToken", "ecr:BatchGetImage",
                 "ecr:GetDownloadUrlForLayer"],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": ["logs:CreateLogStream", "logs:PutLogEvents",
                 "logs:DescribeLogStreams"],
      "Resource": "*"
    }
  ]
}
```

Trust policy principal: `bedrock-agentcore.amazonaws.com`.

**Caller policy** — attached to whoever invokes the agent:

```json
{
  "Effect": "Allow",
  "Action": "bedrock-agentcore:InvokeAgentRuntime",
  "Resource": "arn:aws:bedrock-agentcore:<REGION>:<ACCOUNT_ID>:runtime/<RUNTIME_ID>"
}
```

Scope `bedrock:InvokeModel` down to your specific model or inference-profile ARN once
things work.

## 5. Deployment path

The `bedrock-agentcore-starter-toolkit` provides an `agentcore` CLI that builds the
ARM64 image, creates the ECR repo, and registers the runtime.

```bash
pip install bedrock-agentcore-starter-toolkit

agentcore configure --entrypoint app.py --name sql_test_generator
agentcore launch
agentcore invoke '{"sql": "SELECT * FROM customers"}'
```

`agentcore configure` writes a config file recording the entrypoint, execution role,
and ECR repo. `agentcore launch` builds and deploys. Re-running `launch` after a code
change redeploys.

**The model** comes from `agent.yaml` inside the image. To override per environment
without rebuilding, set a container environment variable:

```bash
BEDROCK_MODEL_ID=eu.amazon.nova-pro-v1:0
```

**Invoking from an application:**

```python
import boto3, json

client = boto3.client("bedrock-agentcore", region_name="eu-central-1")

response = client.invoke_agent_runtime(
    agentRuntimeArn="arn:aws:bedrock-agentcore:...:runtime/...",
    runtimeSessionId="any-stable-id-per-conversation",
    payload=json.dumps({"sql": "SELECT ...", "context": "..."}),
)
print(json.loads(response["response"].read()))
```

**Verify before deploying** — build the image locally and exercise the contract:

```bash
docker build --platform linux/arm64 -t sql-test-generator .
docker run -p 8080:8080 \
  -e BEDROCK_MODEL_ID=eu.amazon.nova-pro-v1:0 \
  -e AWS_REGION=eu-central-1 \
  -v ~/.aws:/root/.aws:ro \
  sql-test-generator

curl localhost:8080/ping
curl -X POST localhost:8080/invocations \
  -H 'Content-Type: application/json' \
  -d @examples/sample_payload.json
```

If `/ping` and `/invocations` both answer locally, the contract is satisfied.

## 6. Is AgentCore the right target?

An honest assessment, since this drives real cost.

**What this workload actually is:** a stateless function. Three sequential Bedrock
calls, no tools, no memory, no multi-turn conversation, no external API credentials.
Each run is independent.

**What Runtime is built for:** long-running, stateful, tool-using agents that need
session persistence across turns, identity brokering, or an 8-hour execution ceiling.

The features you pay for — session isolation, extended runtime, memory, identity —
are all ones this pipeline does not exercise.

| | Lambda | AgentCore Runtime |
|---|---|---|
| Fits a stateless 3-call pipeline | Yes | Yes, but overprovisioned |
| Max duration | 15 min | 8 hours |
| Session state across calls | No | Yes |
| Packaging | zip | ARM64 container + ECR |
| Setup effort | Low | Moderate |

**Recommendation:** if the goal is production hosting of this pipeline as-is, Lambda
is the cheaper and simpler fit — the pipeline finishes in well under 15 minutes and
holds no state. Choose AgentCore Runtime when you intend to grow this into a genuine
agent: adding tool use via Gateway, conversational memory, or interactive multi-turn
refinement of test cases. Those are exactly the cases where Lambda starts fighting you
and Runtime stops being overkill.

If the goal is **learning AgentCore**, deploy it — the migration is genuinely small,
and §3 is the whole of it.

## 7. Verification checklist

- [ ] `docker build --platform linux/arm64` succeeds
- [ ] `GET /ping` returns healthy locally
- [ ] `POST /invocations` returns test cases locally
- [ ] Execution role trusts `bedrock-agentcore.amazonaws.com`
- [ ] `BEDROCK_MODEL_ID` set in the container environment
- [ ] Bedrock model access granted in the deployment region
- [ ] `agentcore invoke` returns test cases
- [ ] CloudWatch shows the three pass log lines

---

**Note on accuracy:** the service contract in §2 is taken from current AWS
documentation. The CLI flags in §5 and the SDK import path in §3 should be confirmed
against the installed package version — the starter toolkit has moved quickly, and
`agentcore configure --help` is the authority over this document.
