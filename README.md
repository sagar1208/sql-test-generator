# SQL Test Generator

Generates plain-English data quality test cases from a SQL query, using a three-pass
pipeline (Understand → Generate → Self-Critique) on AWS Bedrock.

Runs as a local command-line tool. Each run makes three Bedrock calls.

## Setup

Needs Python 3.12+ and a machine where `aws sts get-caller-identity` works.

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Bedrock model access must be enabled in the region you are calling
(Console → Bedrock → Model access → Modify model access).

Then choose a model — this is required, there is no default:

```bash
export BEDROCK_MODEL_ID=eu.amazon.nova-pro-v1:0
export AWS_REGION=eu-central-1          # optional, defaults to eu-central-1
```

## Run it

Pass a SQL query directly:

```bash
python agent.py --sql "SELECT * FROM customers"
```

From a `.sql` file, with business context:

```bash
python agent.py --sql-file query.sql --context "Feeds the marketing campaign system"
```

From a JSON payload, saving the result:

```bash
python agent.py --input examples/sample_payload.json --output result.md
```

Piped through stdin:

```bash
cat query.sql | python agent.py
```

## Options

| Flag | Purpose |
|---|---|
| `--sql` | SQL query as a string |
| `--sql-file` | Path to a file containing the SQL |
| `--input` | Path to a JSON payload, or an array of payloads for batch runs |
| `--context` | Optional business context (ignored with `--input`) |
| `--json` | Print raw JSON instead of formatted markdown |
| `--output` | Write the result to a file instead of the terminal |

`--sql`, `--sql-file`, and `--input` are mutually exclusive. With none of them, the
query is read from stdin. Exit code is `1` if any payload failed.

## Input format

A JSON payload has a required `sql` field and an optional `context` field — see
[examples/sample_payload.json](examples/sample_payload.json). Pass an array of these
objects to `--input` to process several queries in one run.

## Editing the prompts

The prompts and model settings live in [agent.yaml](agent.yaml). Edit them there —
no code change, no redeploy of logic.

```yaml
bedrock:
  model_id: eu.amazon.nova-pro-v1:0
  max_tokens: 4096
  temperature: 0.3

pipeline:
  - name: understand
    prompt: |
      Analyze the following SQL query...
```

Passes run top to bottom, each receiving the previous one's output. Prompts may use
`{sql}`, `{context}`, `{query_analysis}` (output of the first pass), and
`{generated_cases}` (output of the preceding pass).

Any `bedrock` key can be overridden per pass — useful for running the cheap analysis
pass on a smaller model:

```yaml
  - name: understand
    model_id: eu.amazon.nova-lite-v1:0
    prompt: |
      ...
```

Add or remove passes freely; the pipeline is driven entirely by this list.

## Region and model

Both are read from the environment, so no code change is needed to switch.

`BEDROCK_MODEL_ID` is **required** and accepts a model ID, an inference-profile ID, or
a profile ARN. If you use an application inference profile, export its ARN:

```bash
export BEDROCK_MODEL_ID=arn:aws:bedrock:eu-central-1:<ACCOUNT_ID>:application-inference-profile/<PROFILE_ID>
```

Region falls back to `AWS_REGION`, then `AWS_DEFAULT_REGION`, then `eu-central-1`.

| Model ID | Speed | Cost | Quality |
|---|---|---|---|
| `eu.amazon.nova-micro-v1:0` | fastest | lowest | basic |
| `eu.amazon.nova-lite-v1:0` | fast | low | good |
| `eu.amazon.nova-pro-v1:0` | moderate | medium | best of Nova |
| `eu.anthropic.claude-3-5-sonnet-20241022-v2:0` | moderate | highest | highest |

Outside Europe use the `us.` prefix instead of `eu.`.

## Common errors

| Error | Fix |
|---|---|
| `Signature expired` | Machine clock is off — `sudo sntp -sS time.apple.com` |
| `You don't have access to the model` | Enable model access in that region |
| `on-demand throughput isn't supported` | Use the `eu.` / `us.` prefixed model ID |
| `AccessDeniedException ... bedrock:InvokeModel` | Add the permission to your IAM user |
| `Unable to locate credentials` | Run `aws configure` |
| `Bedrock returned no text content` | Raise `maxTokens` or keep reasoning disabled |
| `No Bedrock model configured` | `export BEDROCK_MODEL_ID=...` |
