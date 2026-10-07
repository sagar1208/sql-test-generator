"""Plain-English data-quality test cases for SQL, AWS Glue and Airflow sources.
One model call per request: the agent reads the supplied source and writes test
cards naming only tables a developer can still query after the job has run.
"""
import asyncio
import logging
import os
import re
import secrets
from pathlib import Path
import boto3
from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
from bedrock_agentcore.memory.integrations.strands.session_manager import (
    AgentCoreMemorySessionManager,
)
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from botocore.config import Config
from strands import Agent
from strands.models import BedrockModel
from strands.types.exceptions import MaxTokensReachedException
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
logging.getLogger("botocore").setLevel(logging.ERROR)
app = BedrockAgentCoreApp()
# --- configuration -------------------------------------------------------------------
REGION = (
    os.environ.get("AWS_REGION")
    or os.environ.get("AWS_DEFAULT_REGION")
    or "eu-central-1"
)
# Required, with no default: a missing value must fail the request, not
# silently run on a different model.
MODEL_ID = (
    os.environ.get("BEDROCK_MODEL_ID")
    or os.environ.get("SONNET5_INFERENCE_PROFILE_ARN")
    or ""
).strip()
AGENT_NAME = os.environ.get("AGENT_NAME", "sql_test_agent")
MEMORY_ID = os.environ.get("AGENTCORE_MEMORY_ID", "").strip()
MEMORY_ACTOR_ID = os.environ.get("AGENTCORE_MEMORY_ACTOR_ID", AGENT_NAME)
PROMPTS_DIR = Path(__file__).parent
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{33,100}$")

def _env_int(name: str, default: int, minimum: int) -> int:
    """A tuning value from the environment. A bad value fails at startup, not mid-request."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a whole number, got {raw!r}.") from exc
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}, got {value}.")
    return value

# Overridable through agent.yaml so a truncating job can be handled with a
# terraform apply rather than an image rebuild. The defaults are the tested
# values and should be the same in every environment.
MAX_SOURCE_LENGTH = _env_int("MAX_SOURCE_LENGTH", 150_000, 1_000)   # ~40k tokens
# Headroom, not a target: the prompt caps the answer at MAX_CARDS short cards,
# and a budget this size is what stops a verbose answer being truncated.
MAX_TOKENS = _env_int("MAX_TOKENS", 16_000, 1_000)
MAX_CARDS = _env_int("MAX_CARDS", 5, 1)
MAX_CONTEXT_LENGTH = 50_000
# The prose length the prompt asks for.
CARD_WORD_TARGET = 120
# The reject threshold, counted over the whole card including its field labels,
# so it has to sit above CARD_WORD_TARGET rather than equal it.
MAX_CARD_WORDS = 200
MAX_VALIDATION_RETRIES = 1
# A large source can take minutes to answer; botocore's 60 s default read
# timeout would fail it. Strands retries throttling itself, so botocore is told
# not to retry on top of that.
BEDROCK_CONNECT_TIMEOUT = 10
BEDROCK_READ_TIMEOUT = 300
BEDROCK_MAX_ATTEMPTS = 1
# Payload keys that may carry the source. The invoker Lambda sends "prompt".
SOURCE_FIELDS = ("source", "sql", "glue_job", "airflow_job", "prompt")
OUT_OF_SCOPE = (
    "OUT_OF_SCOPE: this input is not a recognizable SQL query, "
    "AWS Glue ETL job, or Airflow DAG."
)
 
 
def _is_source_input(text: str) -> bool:
    lowered = text.lower()
    return bool(
        re.search(
            r"\b(select|insert\s+into|update\s+\w+\s+set|delete\s+from|with\s+\w+\s+as|create\s+(?:or\s+replace\s+)?(?:table|view))\b",
            lowered,
        )
        or any(
            marker in lowered
            for marker in (
                "awsglue",
                "gluecontext",
                "dynamicframe",
                "getresolvedoptions",
                "from airflow",
                "pythonoperator",
                "@dag",
            )
        )
    )
 
 
def _source_type(text: str) -> str:
    lowered = text.lower()
    if any(marker in lowered for marker in ("awsglue", "gluecontext", "dynamicframe", "getresolvedoptions")):
        return "AWS Glue ETL job"
    if any(marker in lowered for marker in ("from airflow", "pythonoperator", "@dag")):
        return "Airflow DAG"
    if _is_source_input(text):
        return "SQL"
    return "follow-up or unclassified input"
 
 
def _response_details(cards: str, source: str, session_id: str) -> dict[str, object]:
    is_out_of_scope = cards.strip() == OUT_OF_SCOPE
    return {
        "source_type": _source_type(source),
        "test_case_count": 0 if is_out_of_scope else len(_split_cards(cards)),
        "memory": "configured" if MEMORY_ID and session_id else "not_used",
        "validation": "out_of_scope" if is_out_of_scope else "passed",
    }
 
 
def _is_memory_failure(error: Exception) -> bool:
    message = str(error).lower()
    return any(
        marker in message
        for marker in (
            "agentcore memory",
            "memoryid",
            "memory_id",
            "list_events",
            "create_event",
            "accessdeniedexception",
            "serviceunavailableexception",
            "throttlingexception",
        )
    )
def _load_prompts() -> tuple[str, str, str]:
    sections = (PROMPTS_DIR / "system_prompt.md").read_text(encoding="utf-8").split(
        "\n<!-- PROMPT -->\n"
    )
    if len(sections) != 3:
        raise ValueError("system_prompt.md must contain system, task, and repair sections.")
    required = {
        "{nonce}",
        "{source}",
        "{context}",
        "{out_of_scope}",
        "{max_cards}",
        "{max_words}",
    }
    missing = sorted(token for token in required if token not in sections[1])
    if "{feedback}" not in sections[2]:
        missing.append("{feedback}")
    if missing:
        raise ValueError(f"system_prompt.md is missing placeholders: {', '.join(missing)}")
    return tuple(section.strip() for section in sections)
 
 
SYSTEM_PROMPT, TEST_CASE_PROMPT, REPAIR_PROMPT = _load_prompts()
# --- request -------------------------------------------------------------------------

class InvalidPayload(ValueError):
    """A caller error, kept distinct so invoke() does not also swallow internal bugs."""

def read_payload(payload) -> tuple[str, str]:
    """The source and the caller's context. Raises InvalidPayload."""
    if not isinstance(payload, dict):
        raise InvalidPayload("Payload must be a dictionary")
    source = ""
    for field in SOURCE_FIELDS:
        value = payload.get(field)
        if isinstance(value, str) and value.strip():
            source = value.strip()
            break
    if not source:
        raise InvalidPayload(
            f"Missing source content. Supply one of: {', '.join(SOURCE_FIELDS)}."
        )
    if len(source) > MAX_SOURCE_LENGTH:
        raise InvalidPayload(
            f"Source is {len(source)} characters; the maximum is "
            f"{MAX_SOURCE_LENGTH}. Split it into smaller logical units."
        )
    # Default only on None: `or ""` would turn 0, [] or False into "" and skip
    # the type check.
    context = payload.get("context")
    if context is None:
        context = ""
    if not isinstance(context, str):
        raise InvalidPayload("Field 'context' must be a string")
    if len(context) > MAX_CONTEXT_LENGTH:
        raise InvalidPayload(
            f"Context is {len(context)} characters; the maximum is {MAX_CONTEXT_LENGTH}."
        )
    return source, context.strip()

# --- model call ----------------------------------------------------------------------

CARD_START_PATTERN = re.compile(
    r"^\s*(?:[#*>`]+\s*)?TC-(\d+)(?:\s*[-:·—–]\s*|\s+)",
    re.IGNORECASE,
)
TEMP_TABLE_PATTERN = re.compile(
    r"\bcreate\s+(?:or\s+replace\s+)?temp(?:orary)?\s+"
    r"(?:table|view)\s+([\w.#]+)",
    re.IGNORECASE,
)
CTE_PATTERN = re.compile(
    r"(?:\bwith\s+(?:recursive\s+)?|,\s*)([A-Za-z_]\w*)\s+as\s*\(",
    re.IGNORECASE,
)
def _split_cards(text: str) -> list[str]:
    cards = []
    current = []
    for line in text.splitlines():
        if CARD_START_PATTERN.match(line):
            if current:
                cards.append("\n".join(current).strip())
            current = [line]
        elif current:
            current.append(line)
    if current and "\n".join(current).strip():
        cards.append("\n".join(current).strip())
    return cards

# Sections whose body sits on the lines below the heading rather than after a colon.
BLOCK_HEADINGS = ("What to test", "Pass criteria", "Failure means")
FIELD_LABELS = (
    "Category",
    "Priority",
    "Source table",
    "Target table",
    "Key columns",
) + BLOCK_HEADINGS
LABEL_PATTERN = re.compile(
    r"^\s*(?:" + "|".join(re.escape(label) for label in FIELD_LABELS) + r")\s*(?::|$)",
    re.IGNORECASE,
)
 
 
def _section_value(card: str, heading: str) -> str:
    # "Heading" alone on its line, or "Heading: value" on one line.
    pattern = re.compile(
        r"^\s*" + re.escape(heading) + r"\s*(?::\s*(.*))?$", re.IGNORECASE
    )
    lines = card.splitlines()
    for index, line in enumerate(lines):
        match = pattern.match(line)
        if not match:
            continue
        value = (match.group(1) or "").strip()
        if value or heading not in BLOCK_HEADINGS:
            return value
        # The body follows on the next lines, indented or not. The prompt asks
        # for it unindented, so indentation cannot be what marks it. Skip a
        # blank line after the heading; stop at the next blank line or label.
        body = []
        for following in lines[index + 1:]:
            if not following.strip():
                if body:
                    break
                continue
            if LABEL_PATTERN.match(following):
                break
            body.append(following.strip())
        return " ".join(body)
    return ""

def _table_names(value: str) -> list[str]:
    return [name.strip(" `*.;()") for name in value.rstrip(".").split(",") if name.strip()]

def _temporary_objects(source: str) -> set[str]:
    objects = {
        name.lower().lstrip("#")
        for name in TEMP_TABLE_PATTERN.findall(source)
    }
    objects.update(name.lower() for name in CTE_PATTERN.findall(source))
    objects.update(
        name.lower().lstrip("#")
        for name in re.findall(r"(?<!\w)#([A-Za-z_]\w*)", source)
    )
    return objects

def _table_occurs_in_source(name: str, source: str) -> bool:
    pattern = r"(?<![\w.])" + re.escape(name) + r"(?![\w.])"
    return re.search(pattern, source, re.IGNORECASE) is not None

def _validate_cards(text: str, source: str) -> list[str]:
    if text.strip() == OUT_OF_SCOPE:
        return []
    cards = _split_cards(text)
    errors = []
    ids = []
    temporary_objects = _temporary_objects(source)
    required = (
        "Source table",
        "Target table",
        "Key columns",
        "What to test",
        "Pass criteria",
        "Failure means",
    )
    if not cards:
        return ["No test cards were found."]
    if len(cards) > MAX_CARDS:
        errors.append(f"The answer contains {len(cards)} cards; maximum is {MAX_CARDS}.")
    for card in cards:
        match = CARD_START_PATTERN.match(card)
        if not match:
            errors.append("A card does not start with TC-<number>.")
            continue
        card_id = int(match.group(1))
        if card_id in ids:
            errors.append(f"Duplicate card id TC-{card_id:03d}.")
        ids.append(card_id)
        if len(card.split()) > MAX_CARD_WORDS:
            errors.append(f"TC-{card_id:03d} is longer than {MAX_CARD_WORDS} words.")
        for heading in required:
            if not _section_value(card, heading):
                errors.append(f"TC-{card_id:03d} is missing '{heading}'.")
        for heading in ("Source table", "Target table"):
            value = _section_value(card, heading)
            if value.rstrip(" .").lower() == "query output" and heading == "Target table":
                continue
            for name in _table_names(value):
                normalized = name.lower().lstrip("#")
                if normalized in temporary_objects:
                    errors.append(f"TC-{card_id:03d} names a temporary object in '{heading}'.")
                elif _is_source_input(source) and not _table_occurs_in_source(name, source):
                    errors.append(
                        f"TC-{card_id:03d} names '{name}', which does not appear in the source."
                    )
    return errors

def generate(
    source: str,
    context: str,
    session_id: str = "",
) -> str:
    """One model call, returning the test cards as markdown."""
    if not MODEL_ID:
        raise ValueError(
            "No Bedrock model configured. Set BEDROCK_MODEL_ID or "
            "SONNET5_INFERENCE_PROFILE_ARN."
        )
    model = BedrockModel(
        boto_session=boto3.Session(region_name=REGION),
        boto_client_config=Config(
            connect_timeout=BEDROCK_CONNECT_TIMEOUT,
            read_timeout=BEDROCK_READ_TIMEOUT,
            retries={"max_attempts": BEDROCK_MAX_ATTEMPTS, "mode": "standard"},
        ),
        model_id=MODEL_ID,
        max_tokens=MAX_TOKENS,
    )
    # Tags the untrusted source so instructions hidden inside it cannot pass
    # themselves off as the caller's.
    nonce = secrets.token_hex(8)
    prompt = TEST_CASE_PROMPT.format(
        nonce=nonce,
        source=source,
        context=context or "none supplied",
        max_cards=MAX_CARDS,
        max_words=CARD_WORD_TARGET,
        out_of_scope=OUT_OF_SCOPE,
    )
 
    session_manager = None
    if MEMORY_ID and session_id:
        if not SESSION_ID_PATTERN.fullmatch(session_id):
            raise ValueError(
                "runtimeSessionId must be 33-100 characters and contain only "
                "letters, numbers, dots, hyphens, or underscores."
            )
        try:
            session_manager = AgentCoreMemorySessionManager(
                AgentCoreMemoryConfig(
                    memory_id=MEMORY_ID,
                    actor_id=MEMORY_ACTOR_ID,
                    session_id=session_id,
                ),
                region_name=REGION,
            )
        except Exception:  # noqa: BLE001 - memory must not block test generation
            logger.warning("AgentCore Memory unavailable; continuing without memory.", exc_info=True)
    def ask(model_prompt: str) -> str:
        # A new Agent per attempt keeps repair instructions separate from the
        # invalid answer while preserving one request's session attributes.
        agent = Agent(
            model=model,
            system_prompt=SYSTEM_PROMPT.format(nonce=nonce),
            name=AGENT_NAME,
            callback_handler=None,
            trace_attributes={"session.id": session_id} if session_id else {},
            session_manager=session_manager,
        )
        try:
            result = agent(model_prompt)
        except MaxTokensReachedException as exc:
            raise ValueError(
                f"Model output was cut off at {MAX_TOKENS} tokens. The source may be "
                "too large for one request; split it into smaller logical units."
            ) from exc
        except Exception as exc:
            if session_manager is None or not _is_memory_failure(exc):
                raise
            logger.warning(
                "AgentCore Memory failed; retrying this call once without memory.",
                exc_info=True,
            )
            fallback_agent = Agent(
                model=model,
                system_prompt=SYSTEM_PROMPT.format(nonce=nonce),
                name=AGENT_NAME,
                callback_handler=None,
                trace_attributes={"session.id": session_id} if session_id else {},
            )
            result = fallback_agent(model_prompt)
        for item in result.message.get("content") or []:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                if item["text"].strip():
                    return item["text"].strip()
        raise ValueError("Model returned no text.")
    cards = ask(prompt)
    for attempt in range(MAX_VALIDATION_RETRIES + 1):
        errors = _validate_cards(cards, source)
        if not errors:
            return cards
        if attempt == MAX_VALIDATION_RETRIES:
            raise ValueError("Generated test cases failed validation: " + " ".join(errors))
        cards = ask(
            prompt
            + "\n\n"
            + REPAIR_PROMPT.format(
                feedback=" ".join(errors),
                max_cards=MAX_CARDS,
                max_words=CARD_WORD_TARGET,
            )
        )
    raise ValueError("Generated test cases failed validation.")

# --- entrypoint ----------------------------------------------------------------------

def invoke(payload: dict) -> dict:
    """Handle one request. Always returns a dict; failures come back as {"error": ...}."""
    session_id = payload.get("runtimeSessionId") if isinstance(payload, dict) else None
    if not isinstance(session_id, str):
        session_id = ""
    echo = {"runtimeSessionId": session_id} if session_id else {}
    try:
        source, context = read_payload(payload)
    except InvalidPayload as exc:
        return {"error": str(exc), **echo}
    logger.info("Generating test cases from %d characters of source", len(source))
    try:
        cards = generate(source, context, session_id)
    except Exception as exc:
        logger.exception("Request failed")
        return {"error": str(exc), **echo}
    logger.info("Done: %d characters of test cases", len(cards))
    return {
        "test_cases_markdown": cards,
        "details": _response_details(cards, source, session_id),
        **echo,
    }

@app.entrypoint
async def handle(payload=None):
    """AgentCore entrypoint."""
    if payload is None:
        payload = {}
    # invoke() blocks on Bedrock; keep it off the event loop.
    result = await asyncio.to_thread(invoke, payload)
    if "error" in result:
        return {"status": "error", **result}
    return {"status": "success", **result}

if __name__ == "__main__":
    app.run()
