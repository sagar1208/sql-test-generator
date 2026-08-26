# SQL Test Generator — Blueprint

## Overview

A local command-line tool that generates plain-English data quality test cases from a
SQL query, using a three-pass reasoning pipeline powered by AWS Bedrock.

## Architecture

Three sequential Bedrock calls, each feeding the next. The passes below are the
defaults defined in [agent.yaml](agent.yaml) and can be edited or extended there.

**Pass 1: Understand**
- Analyzes SQL query structure and intent
- Identifies target tables, key columns, source tables
- Maps transformations (joins, aggregations, filters, CASE statements)
- Produces a plain-English summary of query logic

**Pass 2: Generate**
- Creates 3-6 data quality test cases in plain English
- Covers structural risks (nulls, types, schema)
- Covers referential integrity risks (joins, lookups)
- Covers business logic risks (calculations, row counts, thresholds)
- No SQL code is written — only descriptions

**Pass 3: Self-Critique**
- Validates test cases for specificity and coverage
- Removes generic boilerplate
- Refines and returns the final test cases

## Project Structure

```
.
├── agent.py                   # CLI entrypoint and pipeline orchestration
├── agent.yaml                 # Agent definition: model settings and prompts
├── config.py                  # agent.yaml loader and validation
├── llm_client.py              # AWS Bedrock Converse API wrapper
├── requirements.txt           # Python dependencies
├── examples/
│   └── sample_payload.json    # Sample input payload
├── pyproject.toml             # Project configuration
├── README.md                  # Usage guide
└── BLUEPRINT.md               # This file
```

## File Specifications

### agent.py

**Library entrypoint:** `invoke(payload: dict) -> dict`
- Input: `sql` (required, non-empty string), `context` (optional string)
- Success: `{"query_summary": "...", "test_cases_markdown": "..."}`
- Error: `{"error": "message"}` — never raises

**CLI entrypoint:** `main() -> int`, invoked via `python agent.py`
- Input sources (mutually exclusive): `--sql`, `--sql-file`, `--input`; stdin if none given
- `--context` supplies business context for `--sql` and `--sql-file`
- `--input` accepts a single JSON object or an array for batch runs
- `--json` emits raw JSON; otherwise a markdown report
- `--output` writes to a file instead of stdout
- Returns exit code `1` if any payload produced an error, `0` otherwise

**Internal helpers:** `_validate_payload`, `_run_pipeline`, `_read_sql`,
`_read_input_payloads`, `_build_argument_parser`, `_format_result`, `_format_results`

Pipeline failures raise `RuntimeError` inside `_run_pipeline` and are converted to an
error dict by `invoke`.

### llm_client.py

**Class:** `LLMClient`
- `__init__(region: str | None = None, model: str | None = None, settings: dict | None = None)`
  where `settings` is a resolved `bedrock` block from `agent.yaml`
- `invoke(prompt: str) -> str`

**API:** Bedrock Converse (`bedrock-runtime.converse`), chosen over `invoke_model`
because the request and response shapes are identical across Nova, Claude, and Llama.

**Configuration resolution:**
- Region: argument → `AWS_REGION` → `AWS_DEFAULT_REGION` → `agent.yaml` → `eu-central-1`
- Model: argument → `BEDROCK_MODEL_ID` → `agent.yaml` → error. No account-specific
  identifier is baked into the source.

**Inference config:** `maxTokens` and `temperature` from `agent.yaml`; reasoning
disabled via `additionalModelRequestFields={"thinking": {"type": "disabled"}}` unless
`thinking` is set otherwise.

**Retries:** botocore `adaptive` mode, `max_attempts` from `agent.yaml`, so Bedrock
throttling is retried with backoff rather than failing the run.

**Failures:** Raises `ValueError`. Invocation errors include the region and model in
the message; empty-content errors include `stopReason` and the content block types.

### agent.yaml

The agent definition. Holds model settings and the prompt for every pass, so prompt
changes need no code edit.

- `bedrock`: `region`, `model_id`, `max_tokens`, `temperature`, `thinking`, `retries`
- `pipeline`: ordered list of passes, each with `name`, `description`, `prompt`, and
  optional overrides of any `bedrock` key

**Template variables:** `{sql}`, `{context}`, `{query_analysis}` (first pass output),
`{generated_cases}` (preceding pass output)

**Precedence:** constructor argument → environment variable → `agent.yaml`

### config.py

Loads and validates `agent.yaml` into `AgentConfig` and `Pass` objects. Raises
`ValueError` naming the file for a missing config, malformed YAML, an empty pipeline,
a pass without a prompt, or a prompt referencing an unknown placeholder.

### requirements.txt

- `boto3>=1.43.0` — Bedrock client
- `PyYAML>=6.0` — parses `agent.yaml`

### examples/sample_payload.json

A realistic query exercising a LEFT JOIN, GROUP BY, HAVING, aggregations, and a CASE
statement for customer segmentation, with business context describing intent and
expected data characteristics.

## Prerequisites

1. Python 3.12+
2. AWS credentials that `aws sts get-caller-identity` accepts
3. Bedrock model access granted **in the region being called** — access is per-region
4. IAM permission for `bedrock:InvokeModel`
5. A model set in `agent.yaml`, or `BEDROCK_MODEL_ID` exported to override it

## Running

```bash
pip install -r requirements.txt
python agent.py --input examples/sample_payload.json --output result.md
```

See [README.md](README.md) for the full set of invocation forms.

## Success Criteria

**Correctness**
- Missing or empty `sql` returns a clear error
- Non-string `context` returns a clear error
- Empty LLM response at any pass returns a clear error naming the pass
- Bedrock access failures name the region and model

**Output quality**
- Query summary captures the main intent
- Test cases are query-specific, not generic boilerplate
- Coverage spans structural, referential, and business logic risks
- All output is plain English with no SQL code

**Robustness**
- Handles queries of varying complexity
- No crashes on malformed input or unreadable files
- Batch runs report per-payload errors without aborting the run

## Limitations & Future Work

- Three sequential model calls per query — latency scales with query complexity
- Test cases are descriptive only; SQL generation is out of scope
- No persistence of history or results

## Environment Variables

| Variable | Required | Example | Purpose |
|---|---|---|---|
| `AWS_REGION` | No | `eu-central-1` | Overrides `bedrock.region`; then `AWS_DEFAULT_REGION`, then `agent.yaml` |
| `BEDROCK_MODEL_ID` | No | `eu.amazon.nova-pro-v1:0` | Overrides `bedrock.model_id` in `agent.yaml` |
| AWS credentials | Yes | — | Standard boto3 resolution (`~/.aws/credentials`, env vars, instance role) |

---

**Status:** Working locally against AWS Bedrock.
