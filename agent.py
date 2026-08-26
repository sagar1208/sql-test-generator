"""Local CLI for SQL data-quality test-case generation.
 
Three-pass reasoning pipeline using the configured LLM client:
  1. Understand: Analyze SQL query structure and intent
  2. Generate: Create plain-English test cases
  3. Self-Critique: Validate and refine test cases
 
Examples:
    python main.py --sql "SELECT * FROM customers"
 
    python main.py --sql-file query.sql
 
./.venv/Scripts/python.exe agent.py --input examples/sample_payload.json --output result.md    
"""
 
import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional
 
import config
from llm_client import LLMClient
 
 
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)
 
 
def _validate_payload(payload: dict) -> tuple[bool, Optional[str]]:
    """Validate required fields in the input payload."""
    if not isinstance(payload, dict):
        return False, "Input payload must be a dictionary"
 
    if "sql" not in payload:
        return False, "Missing required field: 'sql'"
 
    if not isinstance(payload["sql"], str) or not payload["sql"].strip():
        return False, "Field 'sql' must be a non-empty string"
 
    context = payload.get("context", "")
    if context is not None and not isinstance(context, str):
        return False, "Field 'context' must be a string"
 
    return True, None
 
 
def _run_pipeline(sql: str, context: str = "") -> tuple[str, str]:
    """Execute the pipeline defined in agent.yaml.

    Passes run in the order listed under `pipeline`. Each pass receives the
    output of the previous one, so prompts may reference {query_analysis} and
    {generated_cases} as well as {sql} and {context}.

    Args:
        sql: SQL query to analyze.
        context: Optional business context.

    Returns:
        Tuple containing the query analysis and the refined test cases.

    Raises:
        RuntimeError: If configuration, initialization, or an LLM call fails.
    """
    try:
        agent_config = config.load()
    except ValueError as exc:
        raise RuntimeError(f"Failed to load agent config: {exc}") from exc

    # Variables accumulate as passes complete.
    variables = {
        "sql": sql,
        "context": context,
        "query_analysis": "",
        "generated_cases": "",
    }

    outputs: dict[str, str] = {}
    clients: dict[str, LLMClient] = {}

    for index, pipeline_pass in enumerate(agent_config.passes, start=1):
        logger.info(
            "Pass %d (%s): %s",
            index,
            pipeline_pass.name,
            pipeline_pass.description or "running",
        )

        # Reuse one client per distinct model so per-pass overrides work.
        model_id = pipeline_pass.bedrock.get("model_id", "")
        if model_id not in clients:
            try:
                clients[model_id] = LLMClient(settings=pipeline_pass.bedrock)
            except Exception as exc:
                raise RuntimeError(f"Failed to initialize LLM client: {exc}") from exc

        prompt = pipeline_pass.render(**variables)

        try:
            output = clients[model_id].invoke(prompt)
        except Exception as exc:
            raise RuntimeError(
                f"Pass {index} ({pipeline_pass.name}) failed: {exc}"
            ) from exc

        if not output or not output.strip():
            raise RuntimeError(
                f"Pass {index} ({pipeline_pass.name}) produced an empty response"
            )

        output = output.strip()
        outputs[pipeline_pass.name] = output

        # Feed this pass's output forward under both names, so a renamed or
        # reordered pipeline still resolves the documented placeholders.
        variables["query_analysis"] = variables["query_analysis"] or output
        variables["generated_cases"] = output

    summary = outputs.get(agent_config.passes[0].name, "")
    final = outputs[agent_config.passes[-1].name]

    return summary, final


def invoke(payload: dict) -> dict:
    """Generate SQL data-quality test cases locally.
 
    Expected payload:
        {
            "sql": "SELECT ...",
            "context": "Optional business context"
        }
    """
    is_valid, error_message = _validate_payload(payload)
 
    if not is_valid:
        logger.error("Input validation failed: %s", error_message)
        return {"error": error_message}
 
    sql = payload["sql"].strip()
    context = (payload.get("context") or "").strip()
 
    logger.info(
        "Starting test-case generation for SQL query (length=%d)",
        len(sql),
    )
 
    if context:
        logger.info("Context provided: %s", context[:100])
 
    try:
        query_summary, test_cases = _run_pipeline(sql, context)
    except Exception as exc:
        logger.error("Test-case generation failed: %s", exc)
        return {"error": str(exc)}
 
    logger.info("Test-case generation completed successfully")
 
    return {
        "query_summary": query_summary,
        "test_cases_markdown": test_cases,
    }
 
 
def _read_sql(args: argparse.Namespace) -> str:
    """Read SQL from a command-line argument, file, or standard input."""
    if args.sql:
        return args.sql
 
    if args.sql_file:
        sql_path = Path(args.sql_file)
 
        if not sql_path.is_file():
            raise ValueError(f"SQL file does not exist: {sql_path}")
 
        try:
            return sql_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"Could not read SQL file: {exc}") from exc
 
    if not sys.stdin.isatty():
        return sys.stdin.read()
 
    raise ValueError(
        "No SQL supplied. Use --sql, --sql-file, or pipe SQL through stdin."
    )
 
 
def _read_input_payloads(args: argparse.Namespace) -> list[dict]:
    """Read one or more JSON payloads from an input file."""
    input_path = Path(args.input)
 
    if not input_path.is_file():
        raise ValueError(f"Input file does not exist: {input_path}")
 
    try:
        data = json.loads(input_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read JSON input: {exc}") from exc
 
    payloads = data if isinstance(data, list) else [data]
    if not all(isinstance(payload, dict) for payload in payloads):
        raise ValueError("Input JSON must contain an object or an array of objects")
 
    return payloads
 
 
def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate data-quality test cases for a SQL query."
    )
 
    sql_input = parser.add_mutually_exclusive_group()
    sql_input.add_argument(
        "--sql",
        help='SQL query, for example: --sql "SELECT * FROM customers"',
    )
    sql_input.add_argument(
        "--sql-file",
        help="Path to a file containing the SQL query",
    )
    sql_input.add_argument(
        "--input",
        help="Path to a JSON payload or an array of JSON payloads",
    )
 
    parser.add_argument(
        "--context",
        default="",
        help="Optional business context for the SQL query",
    )
 
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Print the result as JSON instead of formatted text",
    )
 
    parser.add_argument(
        "--output",
        help="Optional path where the generated result should be saved",
    )
 
    return parser
 
 
def _format_result(result: dict, as_json: bool) -> str:
    """Format the result for terminal or file output."""
    if as_json:
        return json.dumps(result, indent=2, ensure_ascii=False)
 
    if "error" in result:
        return f"Error: {result['error']}"
 
    return (
        "# Query Summary\n\n"
        f"{result['query_summary']}\n\n"
        "# Data Quality Test Cases\n\n"
        f"{result['test_cases_markdown']}\n"
    )
 
 
def _format_results(results: list[dict], as_json: bool) -> str:
    """Format results from one or more input payloads."""
    if as_json:
        return json.dumps(results, indent=2, ensure_ascii=False)
 
    return "\n\n".join(
        f"# Result {index}\n\n{_format_result(result, as_json=False)}"
        for index, result in enumerate(results, start=1)
    )
 
 
def main() -> int:
    """Local command-line entrypoint."""
    parser = _build_argument_parser()
    args = parser.parse_args()
 
    if args.input:
        try:
            payloads = _read_input_payloads(args)
        except ValueError as exc:
            parser.error(str(exc))
    else:
        try:
            sql = _read_sql(args)
        except ValueError as exc:
            parser.error(str(exc))
 
        payloads = [
            {
                "sql": sql,
                "context": args.context,
            }
        ]
 
    results = [invoke(payload) for payload in payloads]
    output = _format_results(results, as_json=args.json_output)
 
    if args.output:
        output_path = Path(args.output)
 
        try:
            output_path.write_text(output, encoding="utf-8")
        except OSError as exc:
            logger.error("Could not write output file: %s", exc)
            return 1
 
        logger.info("Result written to %s", output_path)
    else:
        print(output)
 
    return 1 if any("error" in result for result in results) else 0
 
 
if __name__ == "__main__":
    raise SystemExit(main())
