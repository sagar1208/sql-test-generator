"""AWS Lambda entry point that triggers SQL data-quality test generation via Amazon Bedrock.

Self-contained for copy-paste into the Lambda console inline code editor: only depends
on boto3, which ships with the standard Python Lambda runtime (no extra layers needed).

Required setup on the Lambda function itself (not controllable from code):
- Environment variables: AWS_REGION, BEDROCK_MODEL_ID (and optionally LOG_LEVEL,
  BEDROCK_TEMPERATURE) under Configuration > Environment variables.
- Execution role: must allow bedrock:InvokeModel on the target model/inference profile ARN.
- Timeout: must exceed worst-case retry backoff (1.5s + 3s + 6s = 10.5s) plus model
  latency across up to 4 attempts. 60s is a safe minimum (default 3s will fail).
"""

import logging
import os
import time
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

AWS_REGION: str = os.getenv("AWS_REGION", "us-east-1")
BEDROCK_MODEL_ID: str = os.getenv("BEDROCK_MODEL_ID", "us.amazon.nova-micro-v1:0")
BEDROCK_TEMPERATURE: float = float(os.getenv("BEDROCK_TEMPERATURE", "0.2"))
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

RETRY_DELAYS_SECONDS: tuple[float, ...] = (1.5, 3.0, 6.0)
RETRYABLE_ERROR_CODES: frozenset[str] = frozenset(
    {"ThrottlingException", "ServiceUnavailableException", "InternalServerException"}
)

logger = logging.getLogger(__name__)
logger.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))


class BedrockInvocationError(Exception):
    """Raised when a Bedrock model invocation fails after retries are exhausted or on a non-retryable error."""


def build_prompt(source_code: str) -> str:
    """Construct the prompt instructing the model to generate SQL data-quality test cases.

    Args:
        source_code: The contents of the SQL/Glue/Airflow job source to analyze.

    Returns:
        The full prompt string to send to the model.
    """
    return f"""You are a data quality engineer. Analyze the following SQL/Glue/Airflow job source code.

Your task:
1. Identify the target table(s) being written to and their key columns.
2. Identify data-quality risks, including but not limited to: null values in
   required columns, duplicate rows/keys, referential integrity violations
   against source tables, row-count sanity (e.g. unexpected drops in volume),
   type mismatches, and date range validity.
3. Generate between 3 and 6 SQL assertion test cases. Each test must be a
   runnable SELECT statement that returns rows ONLY WHEN the data-quality
   check is VIOLATED (i.e. an empty result set means the check passed).

Output requirements:
- Return valid SQL only. Do not include any prose, explanation, or markdown
  fences outside the SQL.
- Immediately above each SELECT statement, include exactly one single-line SQL
  comment (starting with --) explaining what that test checks.

Source job:
```
{source_code}
```
"""


def invoke_bedrock(prompt: str) -> str:
    """Invoke the configured Bedrock model with the given prompt and return its text response.

    Retries up to 3 times with exponential backoff on transient errors
    (ThrottlingException, ServiceUnavailableException, InternalServerException).
    Disables botocore's own built-in retry (max_attempts=1) so this loop is the
    only retry layer, keeping the number of real Bedrock calls predictable.

    Args:
        prompt: The user prompt to send to the model.

    Returns:
        The text content of the model's response.

    Raises:
        BedrockInvocationError: If retries are exhausted, or on any non-retryable
            ClientError (e.g. bad input, auth failure).
    """
    client = boto3.client(
        "bedrock-runtime",
        region_name=AWS_REGION,
        config=Config(retries={"max_attempts": 1, "mode": "standard"}),
    )

    last_error: ClientError | None = None
    attempts = len(RETRY_DELAYS_SECONDS) + 1
    for attempt in range(1, attempts + 1):
        try:
            response = client.converse(
                modelId=BEDROCK_MODEL_ID,
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                inferenceConfig={"temperature": BEDROCK_TEMPERATURE},
            )
            return response["output"]["message"]["content"][0]["text"]
        except ClientError as error:
            error_code = error.response.get("Error", {}).get("Code", "")
            last_error = error
            if error_code not in RETRYABLE_ERROR_CODES:
                raise BedrockInvocationError(
                    f"Bedrock invocation failed with non-retryable error: {error_code}"
                ) from error

            if attempt <= len(RETRY_DELAYS_SECONDS):
                delay = RETRY_DELAYS_SECONDS[attempt - 1]
                logger.warning(
                    "Bedrock invocation attempt %d/%d failed with %s, retrying in %.1fs",
                    attempt,
                    attempts,
                    error_code,
                    delay,
                )
                time.sleep(delay)

    raise BedrockInvocationError(
        f"Bedrock invocation failed after {attempts} attempts: {last_error}"
    ) from last_error


def parse_event(event: dict[str, Any]) -> tuple[str, str]:
    """Extract the source filename and job source code from the Lambda invocation event.

    Args:
        event: The raw Lambda event payload. Must contain a "source_code" key holding
            the SQL/Glue/Airflow job text, and may contain a "source_filename" key
            used only for logging/labeling the response.

    Returns:
        A (source_filename, source_code) tuple.

    Raises:
        ValueError: If "source_code" is missing or empty.
    """
    source_code = event.get("source_code", "")
    if not source_code:
        raise ValueError('Event payload must include a non-empty "source_code" field')
    source_filename = event.get("source_filename", "input.sql")
    return source_filename, source_code


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """AWS Lambda entry point: generate SQL data-quality tests for the job passed in the event.

    Expected event shape (e.g. as a Lambda console test event):
        {
          "source_filename": "sample_orders_job.sql",
          "source_code": "INSERT INTO orders_summary ..."
        }

    Args:
        event: The Lambda invocation event containing the source SQL job.
        context: The Lambda context object (unused, required by the Lambda runtime contract).

    Returns:
        A dict with "source_filename" and "generated_sql" keys. Errors are not caught
        here: they propagate so the invocation shows as a failed execution with a full
        traceback in CloudWatch Logs, which is the standard way to debug a Lambda.
    """
    source_filename, source_code = parse_event(event)
    logger.info("Received job: %s (%d chars)", source_filename, len(source_code))

    prompt = build_prompt(source_code)

    logger.info("Invoking Bedrock model %s in %s", BEDROCK_MODEL_ID, AWS_REGION)
    generated_sql = invoke_bedrock(prompt)
    logger.info("Received response from Bedrock (%d chars)", len(generated_sql))

    return {"source_filename": source_filename, "generated_sql": generated_sql}
