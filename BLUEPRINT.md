# SQL Test Generator — Blueprint

## Overview

A local command-line tool that generates plain-English data quality test cases from a
SQL query, using a three-pass reasoning pipeline powered by AWS Bedrock.

## Architecture

Three sequential Bedrock calls, each feeding the next.

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
├── llm_client.py              # AWS Bedrock Converse API wrapper
├── prompts.py                 # Three prompt templates
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
- `__init__(region: str | None = None, model: str | None = None)`
- `invoke(prompt: str) -> str`

**API:** Bedrock Converse (`bedrock-runtime.converse`), chosen over `invoke_model`
because the request and response shapes are identical across Nova, Claude, and Llama.

**Configuration resolution:**
- Region: argument → `AWS_REGION` → `AWS_DEFAULT_REGION` → `eu-central-1`
- Model: argument → `BEDROCK_MODEL_ID` → error. No default, so no account-specific
  identifier is baked into the source.

**Inference config:** `maxTokens: 4096`, reasoning disabled via
`additionalModelRequestFields={"thinking": {"type": "disabled"}}`.

**Failures:** Raises `ValueError`. Invocation errors include the region and model in
the message; empty-content errors include `stopReason` and the content block types.

### prompts.py

**Constants:** `UNDERSTAND_PROMPT`, `GENERATE_PROMPT`, `SELF_CRITIQUE_PROMPT`

**Template variables:** `{sql}`, `{context}`, `{query_analysis}` (pass 1 output, used
by pass 2), `{generated_cases}` (pass 2 output, used by pass 3)

### requirements.txt

- `boto3>=1.43.0` — Bedrock client, the only runtime dependency

### examples/sample_payload.json

A realistic query exercising a LEFT JOIN, GROUP BY, HAVING, aggregations, and a CASE
statement for customer segmentation, with business context describing intent and
expected data characteristics.

## Prerequisites

1. Python 3.12+
2. AWS credentials that `aws sts get-caller-identity` accepts
3. Bedrock model access granted **in the region being called** — access is per-region
4. IAM permission for `bedrock:InvokeModel`
5. `BEDROCK_MODEL_ID` exported — there is no default model

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
- No retry or backoff on Bedrock throttling

## Environment Variables

| Variable | Required | Example | Purpose |
|---|---|---|---|
| `AWS_REGION` | No | `eu-central-1` | Bedrock region; falls back to `AWS_DEFAULT_REGION`, then `eu-central-1` |
| `BEDROCK_MODEL_ID` | Yes | `eu.amazon.nova-pro-v1:0` | Model ID, inference-profile ID, or profile ARN |
| AWS credentials | Yes | — | Standard boto3 resolution (`~/.aws/credentials`, env vars, instance role) |

---

**Status:** Working locally against AWS Bedrock.
