"""AWS Bedrock AgentCore entrypoint for SQL data quality test case generation.

Three-pass reasoning pipeline:
  1. Understand: Analyze SQL query structure and intent
  2. Generate: Create plain-English test cases
  3. Self-Critique: Validate and refine test cases
"""

import os
import logging
from bedrock_agentcore import BedrockAgentCoreApp
from dotenv import load_dotenv

from llm_client import LLMClient
import prompts

load_dotenv(dotenv_path=".env.local")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = BedrockAgentCoreApp()


def _validate_payload(payload: dict) -> tuple[bool, str | None]:
    """Validate required fields in the payload.

    Returns:
        Tuple of (is_valid, error_message)
    """
    if "sql" not in payload:
        return False, "Missing required field: 'sql'"
    if not isinstance(payload["sql"], str) or not payload["sql"].strip():
        return False, "Field 'sql' must be a non-empty string"
    return True, None


def _run_pipeline(sql: str, context: str = "") -> tuple[str, str]:
    """Execute the three-pass reasoning pipeline.

    Args:
        sql: The SQL query to analyze
        context: Optional business context

    Returns:
        Tuple of (query_summary, test_cases)

    Raises:
        ValueError: If any LLM invocation fails
    """
    try:
        llm = LLMClient()
    except ValueError as e:
        raise ValueError(f"Failed to initialize LLM client: {e}") from e

    # Pass 1: Understand
    logger.info("Pass 1: Understanding SQL query structure...")
    understand_prompt = prompts.UNDERSTAND_PROMPT.format(sql=sql, context=context)
    try:
        query_analysis = llm.invoke(understand_prompt)
    except Exception as e:
        raise ValueError(f"Pass 1 (Understand) failed: {e}") from e

    if not query_analysis or not query_analysis.strip():
        raise ValueError("Pass 1 (Understand) produced empty response")

    # Pass 2: Generate
    logger.info("Pass 2: Generating test cases...")
    generate_prompt = prompts.GENERATE_PROMPT.format(
        sql=sql, context=context, query_analysis=query_analysis
    )
    try:
        test_cases = llm.invoke(generate_prompt)
    except Exception as e:
        raise ValueError(f"Pass 2 (Generate) failed: {e}") from e

    if not test_cases or not test_cases.strip():
        raise ValueError("Pass 2 (Generate) produced empty response")

    # Pass 3: Self-Critique
    logger.info("Pass 3: Self-critiquing and refining test cases...")
    critique_prompt = prompts.SELF_CRITIQUE_PROMPT.format(
        generated_cases=test_cases, sql=sql
    )
    try:
        refined_cases = llm.invoke(critique_prompt)
    except Exception as e:
        raise ValueError(f"Pass 3 (Self-Critique) failed: {e}") from e

    if not refined_cases or not refined_cases.strip():
        raise ValueError("Pass 3 (Self-Critique) produced empty response")

    return query_analysis, refined_cases


@app.entrypoint
def invoke(payload: dict) -> dict:
    """Bedrock AgentCore entrypoint for SQL test case generation.

    Expected payload:
    {
        "sql": "SELECT ... FROM ... WHERE ...",
        "context": "Optional business context string"
    }

    Returns:
    {
        "query_summary": "Plain English description of what the query does",
        "test_cases_markdown": "Numbered list of data quality test cases in plain English"
    }
    Or on error:
    {
        "error": "Error message describing what went wrong"
    }
    """
    # Validate payload
    is_valid, error_msg = _validate_payload(payload)
    if not is_valid:
        logger.error("Payload validation failed: %s", error_msg)
        return {"error": error_msg}

    sql = payload["sql"].strip()
    context = payload.get("context", "").strip()

    logger.info("Starting test case generation for SQL query (length=%d)", len(sql))
    if context:
        logger.info("Provided context: %s", context[:100])

    try:
        query_summary, test_cases = _run_pipeline(sql, context)

        logger.info("Test case generation completed successfully")
        return {
            "query_summary": query_summary,
            "test_cases_markdown": test_cases,
        }

    except ValueError as e:
        logger.error("Test case generation failed: %s", e)
        return {"error": str(e)}
    except Exception as e:
        logger.error("Unexpected error during test case generation: %s", e)
        return {"error": f"Unexpected error: {str(e)}"}


if __name__ == "__main__":
    app.run()
