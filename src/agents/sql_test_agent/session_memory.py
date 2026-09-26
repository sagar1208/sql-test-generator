"""Short-term session memory for the sql_test_agent, using AgentCore Memory.

The memory store is created by terraform from the `memory` block in agent.yaml
and its id reaches this container as MEMORY_ID. Each successful request is saved
as one event under actor + session, and the latest event is read back when the
same session id returns, so the model can keep its earlier test cases in view.
"""

import hashlib
import logging
import os
import re
from datetime import datetime
from typing import NamedTuple

from bedrock_agentcore.memory import MemoryClient

logger = logging.getLogger(__name__)

# --- configuration -------------------------------------------------------------------

REGION = (
    os.environ.get("AWS_REGION")
    or os.environ.get("AWS_DEFAULT_REGION")
    or "eu-central-1"
)

# The store terraform created. Empty means the agent runs without memory.
MEMORY_ID = os.environ.get("MEMORY_ID", "").strip()

# Who the memory belongs to. Every event is filed under actor + session.
ACTOR_ID = os.environ.get("AGENT_NAME", "sql_test_agent")

# Only the start of the source is stored, as a reminder of what was tested.
# The full source arrives with every request, and its hash says whether it changed.
SOURCE_EXCERPT_CHARS = 300

# --- client --------------------------------------------------------------------------

_client = None


def memory_client() -> MemoryClient:
    """The AgentCore Memory client, created once per container and reused."""
    global _client
    if _client is None:
        _client = MemoryClient(region_name=REGION)
    return _client


# --- save ----------------------------------------------------------------------------


def source_fingerprint(source: str) -> str:
    """A fixed-length id for the source: same text, same fingerprint."""
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def save_test_cases(session_id: str, source: str, test_cases: str) -> str:
    """Save one successful request as a memory event and return its event id."""
    event = memory_client().create_event(
        memory_id=MEMORY_ID,
        actor_id=ACTOR_ID,
        session_id=session_id,
        messages=[
            (source[:SOURCE_EXCERPT_CHARS], "USER"),
            (test_cases, "ASSISTANT"),
        ],
        metadata={"source_sha256": {"stringValue": source_fingerprint(source)}},
    )
    return event["eventId"]


# --- load ----------------------------------------------------------------------------


class PreviousRun(NamedTuple):
    """The latest saved request for a session."""

    test_cases: str
    source_changed: bool
    saved_at: datetime


def load_previous_test_cases(session_id: str, source: str) -> PreviousRun | None:
    """The newest saved test cases for this session, or None when there are none."""
    events = memory_client().list_events(
        memory_id=MEMORY_ID,
        actor_id=ACTOR_ID,
        session_id=session_id,
    )
    if not events:
        return None

    # Sorted here because the API does not document the order it returns.
    latest = max(events, key=lambda event: event["eventTimestamp"])

    test_cases = ""
    for item in latest.get("payload", []):
        message = item.get("conversational", {})
        if message.get("role") == "ASSISTANT":
            test_cases = message.get("content", {}).get("text", "")
    if not test_cases:
        return None

    # An event without a fingerprint counts as changed, so the model re-checks everything.
    saved_fingerprint = (
        latest.get("metadata", {}).get("source_sha256", {}).get("stringValue", "")
    )
    return PreviousRun(
        test_cases=test_cases,
        source_changed=saved_fingerprint != source_fingerprint(source),
        saved_at=latest["eventTimestamp"],
    )


# --- safe wrappers: the only functions the agent calls -------------------------------

# AgentCore Memory accepts 1-100 characters and no dots, which is stricter than
# the runtime's own session id rules.
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")


def memory_enabled(session_id: str) -> bool:
    """Memory is used only when a store is configured and the session id is usable."""
    if not MEMORY_ID or not session_id:
        return False
    if not SESSION_ID_PATTERN.match(session_id):
        logger.warning("Session id is not valid for AgentCore Memory; skipping memory.")
        return False
    return True


def safe_load(session_id: str, source: str) -> PreviousRun | None:
    """load_previous_test_cases, or None when memory is off or fails."""
    if not memory_enabled(session_id):
        return None
    try:
        previous = load_previous_test_cases(session_id, source)
    except Exception as exc:  # noqa: BLE001 - no history is better than no answer
        logger.warning("Could not read session memory (%s); continuing without it.", exc)
        return None
    if previous:
        logger.info(
            "Loaded test cases saved at %s; source %s since then",
            previous.saved_at,
            "changed" if previous.source_changed else "unchanged",
        )
    return previous


def safe_save(session_id: str, source: str, test_cases: str) -> None:
    """save_test_cases, never raising: the answer is already correct."""
    if not memory_enabled(session_id):
        return
    try:
        event_id = save_test_cases(session_id, source, test_cases)
    except Exception as exc:  # noqa: BLE001 - never fail a request over memory
        logger.warning("Could not write session memory (%s); continuing.", exc)
        return
    logger.info("Saved test cases to session memory as event %s", event_id)
