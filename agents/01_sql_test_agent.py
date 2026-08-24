"""Generate plain-English data-quality test case descriptions from SQL/Glue/Airflow job files using AWS Bedrock."""

import logging
import os
import sys
import time
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from dotenv import load_dotenv

load_dotenv()

AWS_REGION: str = os.getenv("AWS_REGION", "eu-central-1")
BEDROCK_MODEL_ID: str = os.getenv("BEDROCK_MODEL_ID", "amazon.nova-micro-v1:0")
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
BEDROCK_TEMPERATURE: float = float(os.getenv("BEDROCK_TEMPERATURE", "0.2"))

SQL_JOBS_DIR: Path = Path(os.getenv("SQL_JOBS_DIR", "sql_jobs"))
GENERATED_TESTS_DIR: Path = Path(os.getenv("GENERATED_TESTS_DIR", "generated_tests"))

RETRY_DELAYS_SECONDS: tuple[float, ...] = (1.5, 3.0, 6.0)
RETRYABLE_ERROR_CODES: frozenset[str] = frozenset(
    {"ThrottlingException", "ServiceUnavailableException", "InternalServerException"}
)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


class BedrockInvocationError(Exception):
    """Raised when a Bedrock model invocation fails after retries are exhausted or on a non-retryable error."""


def read_source_file(path: Path) -> str:
    """Read and return the contents of the source job file at the given path.

    Raises:
        FileNotFoundError: If the file does not exist.
    """
    if not path.exists():
        raise FileNotFoundError(f"Source file not found: {path}")
    return path.read_text(encoding="utf-8")


def resolve_source_path(cli_args: list[str]) -> Path:
    """Determine which source file to read: an explicit CLI argument, or the first file in SQL_JOBS_DIR.

    Args:
        cli_args: The process argv list (sys.argv). If a second element is present,
            it is treated as the filename to load, resolved relative to SQL_JOBS_DIR
            if it isn't an existing absolute/relative path on its own.

    Raises:
        FileNotFoundError: If SQL_JOBS_DIR does not exist or contains no files and no
            explicit filename was provided.
    """
    if len(cli_args) > 1:
        candidate = Path(cli_args[1])
        if not candidate.is_absolute() and not candidate.exists():
            candidate = SQL_JOBS_DIR / candidate
        return candidate

    if not SQL_JOBS_DIR.exists():
        raise FileNotFoundError(f"Source directory not found: {SQL_JOBS_DIR}")

    candidates = sorted(p for p in SQL_JOBS_DIR.iterdir() if p.is_file() and p.name != ".gitkeep")
    if not candidates:
        raise FileNotFoundError(f"No source files found in {SQL_JOBS_DIR}")
    return candidates[0]


def build_prompt(source_code: str) -> str:
    """Construct the prompt instructing the model to describe data-quality test cases in plain English.

    Args:
        source_code: The contents of the SQL/Glue/Airflow job file to analyze.

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
3. Describe between 3 and 6 data-quality test cases in plain English. Each
   test case will later be handed to a separate agent that writes the actual
   SQL, so do NOT write any SQL yourself.

Output requirements:
- Return plain English only. No SQL, no code blocks, no markdown fences.
- Output a numbered list. For each item, state: the table and column(s)
  involved, the specific risk being checked (e.g. null check, duplicate
  check, referential integrity, row-count sanity, type mismatch, date range
  validity), and what condition would count as a failure.

Source job:
```
{source_code}
```
"""


def invoke_bedrock(prompt: str) -> str:
    """Invoke the configured Bedrock model with the given prompt and return its text response.

    Retries up to 3 times with exponential backoff on transient errors
    (ThrottlingException, ServiceUnavailableException, InternalServerException).

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


def write_output_file(source_filename: str, generated_test_cases: str) -> Path:
    """Write generated plain-English test case descriptions to generated_tests/<source_filename_without_ext>_test_cases.txt.

    Args:
        source_filename: The name of the source file the test cases were generated from.
        generated_test_cases: The plain-English test case descriptions to write.

    Returns:
        The path to the written output file.
    """
    GENERATED_TESTS_DIR.mkdir(parents=True, exist_ok=True)
    stem = Path(source_filename).stem
    output_path = GENERATED_TESTS_DIR / f"{stem}_test_cases.txt"
    output_path.write_text(generated_test_cases, encoding="utf-8")
    return output_path


def main() -> None:
    """Orchestrate reading a source job file, generating test case descriptions via Bedrock, and writing them out."""
    try:
        source_path = resolve_source_path(sys.argv)
        source_code = read_source_file(source_path)
        logger.info("Loaded source file: %s", source_path)

        prompt = build_prompt(source_code)

        logger.info("Invoking Bedrock model %s in %s", BEDROCK_MODEL_ID, AWS_REGION)
        generated_test_cases = invoke_bedrock(prompt)
        logger.info("Received response from Bedrock (%d chars)", len(generated_test_cases))

        output_path = write_output_file(source_path.name, generated_test_cases)
        logger.info("Wrote generated test cases to: %s", output_path)

        print(output_path)
    except Exception as error:
        logger.error("Test case generation failed: %s", error)
        sys.exit(1)


if __name__ == "__main__":
    main()
