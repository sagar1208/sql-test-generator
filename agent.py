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
 
from llm_client import LLMClient
import prompts
 
 
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
    """Execute the three-pass reasoning pipeline.
 
    Args:
        sql: SQL query to analyze.
        context: Optional business context.
 
    Returns:
        Tuple containing the query analysis and refined test cases.
 
    Raises:
        RuntimeError: If initialization or an LLM invocation fails.
    """
    try:
        llm = LLMClient()
    except Exception as exc:
        raise RuntimeError(f"Failed to initialize LLM client: {exc}") from exc
 
    # Pass 1: Understand
    logger.info("Pass 1: Understanding SQL query structure...")
    understand_prompt = prompts.UNDERSTAND_PROMPT.format(
        sql=sql,
        context=context,
    )
 
    try:
        query_analysis = llm.invoke(understand_prompt)
    except Exception as exc:
        raise RuntimeError(f"Pass 1 (Understand) failed: {exc}") from exc
 
    if not query_analysis or not query_analysis.strip():
        raise RuntimeError("Pass 1 (Understand) produced an empty response")
 
    # Pass 2: Generate
    logger.info("Pass 2: Generating test cases...")
    generate_prompt = prompts.GENERATE_PROMPT.format(
        sql=sql,
        context=context,
        query_analysis=query_analysis,
    )
 
    try:
        test_cases = llm.invoke(generate_prompt)
    except Exception as exc:
        raise RuntimeError(f"Pass 2 (Generate) failed: {exc}") from exc
 
    if not test_cases or not test_cases.strip():
        raise RuntimeError("Pass 2 (Generate) produced an empty response")
 
    # Pass 3: Self-Critique
    logger.info("Pass 3: Self-critiquing and refining test cases...")
    critique_prompt = prompts.SELF_CRITIQUE_PROMPT.format(
        generated_cases=test_cases,
        sql=sql,
    )
 
    try:
        refined_cases = llm.invoke(critique_prompt)
    except Exception as exc:
        raise RuntimeError(f"Pass 3 (Self-Critique) failed: {exc}") from exc
 
    if not refined_cases or not refined_cases.strip():
        raise RuntimeError("Pass 3 (Self-Critique) produced an empty response")
 
    return query_analysis.strip(), refined_cases.strip()
 
 
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
