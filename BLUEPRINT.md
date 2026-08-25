# SQL Test Generator - AgentCore Blueprint

## Overview
A simple AWS Bedrock AgentCore agent that generates plain-English data quality test cases from SQL queries using a three-pass reasoning pipeline powered by Groq.

## Architecture

### Three-Pass Reasoning Pipeline

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
- No SQL code is written—only descriptions

**Pass 3: Self-Critique**
- Validates test cases for specificity and coverage
- Checks for generic boilerplate (removed)
- Ensures all cases are query-specific
- Refines and returns final test cases

## Project Structure

```
.
├── agent.py                    # Bedrock AgentCore entrypoint
├── llm_client.py              # Groq API wrapper
├── prompts.py                 # Three prompt templates
├── requirements.txt           # Python dependencies
├── .env.local.example         # Example environment file
├── examples/
│   └── sample_payload.json   # Sample test payload
├── pyproject.toml            # Project configuration
└── BLUEPRINT.md              # This file
```

## File Specifications

### agent.py
- **Entrypoint:** `invoke(payload: dict) -> dict`
- **Pattern:** BedrockAgentCoreApp with @app.entrypoint decorator
- **Input:** 
  - `sql` (required): SQL query string to analyze
  - `context` (optional): Business context for the query
- **Output:**
  - Success: `{"query_summary": "...", "test_cases_markdown": "..."}`
  - Error: `{"error": "error message"}`
- **No local file I/O:** All input/output via payload/response
- **Error handling:** Returns error dict instead of raising exceptions

### llm_client.py
- **Class:** `LLMClient`
- **Methods:**
  - `__init__(api_key: str | None = None, model: str = "mixtral-8x7b-32768")`
  - `invoke(prompt: str) -> str`
- **API:** Groq (via environment variable GROQ_API_KEY)
- **Failures:** Raises ValueError with clear messages

### prompts.py
- **Constants:**
  - `UNDERSTAND_PROMPT`: First pass template
  - `GENERATE_PROMPT`: Second pass template
  - `SELF_CRITIQUE_PROMPT`: Third pass template
- **Template variables:**
  - `{sql}`: The SQL query
  - `{context}`: Optional business context
  - `{query_analysis}`: Output from pass 1 (for pass 2)
  - `{generated_cases}`: Output from pass 2 (for pass 3)

### requirements.txt
- `groq>=0.4.0`
- `bedrock-agentcore>=1.0.0`
- `python-dotenv>=1.2.3`

### .env.local.example
```
GROQ_API_KEY=your_groq_api_key_here
```

### examples/sample_payload.json
- Contains a realistic SQL query with:
  - JOIN operation (left join)
  - GROUP BY clause
  - CASE statement for segmentation
  - Aggregations (COUNT, SUM)
- Includes business context describing query intent and success criteria

## Deployment: AWS Bedrock AgentCore Runtime

### Prerequisites
1. AWS credentials configured (typically `~/.aws/credentials` or environment variables)
2. AgentCore CLI installed: `pip install bedrock-agentcore`
3. Groq API key obtained from https://console.groq.com
4. `.env.local` file with `GROQ_API_KEY` set

### Local Testing
```bash
# Install dependencies
pip install -r requirements.txt

# Copy .env.local.example to .env.local and add your Groq API key
cp .env.local.example .env.local
# Edit .env.local with your actual GROQ_API_KEY

# Test locally (depends on bedrock_agentcore SDK version)
python agent.py  # or agentcore run agent.py (check SDK CLI)
```

### Deployment Commands
```bash
# (Replace with actual AgentCore CLI commands once SDK documentation is verified)
# Typical pattern:
agentcore deploy --name sql-test-generator --file agent.py
agentcore invoke sql-test-generator < examples/sample_payload.json
```

## Testing

### Local dry-run test
```bash
# Create .env.local with GROQ_API_KEY
# Run the agent directly with sample payload
python -c "
from agent import invoke
import json
with open('examples/sample_payload.json') as f:
    payload = json.load(f)
result = invoke(payload)
print(json.dumps(result, indent=2))
"
```

### Via HTTP (if exposed by AgentCore)
```bash
curl -X POST http://localhost:8000/invoke \
  -H 'Content-Type: application/json' \
  -d @examples/sample_payload.json
```

## Success Criteria

1. **Correctness:**
   - Missing `sql` field returns clear error
   - Missing/invalid GROQ_API_KEY returns clear error
   - Empty LLM response at any pass returns clear error

2. **Output Quality:**
   - Query summary captures main intent
   - Test cases are query-specific (not generic)
   - Test cases cover structural, referential, and business logic risks
   - All output is plain English (no SQL code)

3. **Robustness:**
   - Handles queries of varying complexity
   - Graceful error messages on LLM failures
   - No crashes on malformed input

## Limitations & Future Work

- Single SQL query per invocation (no multi-query batching)
- Uses Groq (mixtral-8x7b-32768) instead of proprietary Bedrock models
- Test cases are descriptive only (SQL generation handled by separate agent)
- No persistence (no stored history or results)

## Environment Variables

| Variable | Required | Example | Purpose |
|----------|----------|---------|---------|
| GROQ_API_KEY | Yes | `gsk_...` | Groq API authentication |
| LOG_LEVEL | No | `INFO` | Logging level (INFO, DEBUG, WARNING) |

---

**Status:** Initial implementation ready for AgentCore runtime deployment.
