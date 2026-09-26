"""Bedrock model and Strands agent construction, shared by every TINA agent.

One place decides how a model is called: the timeouts, the retry policy, how a
truncated answer is reported, and how text is pulled back out of a Strands
result. An agent that builds its own client would drift from these on the first
copy-paste, and the drift would only show up under load.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

import boto3
from botocore.config import Config
from strands import Agent
from strands.models import BedrockModel
from strands.types.exceptions import MaxTokensReachedException

logger = logging.getLogger(__name__)

# A large source can take minutes to answer; botocore's 60 s default read
# timeout would fail it. Strands retries throttling itself, so botocore is told
# not to retry on top of that.
BEDROCK_CONNECT_TIMEOUT = 10
BEDROCK_READ_TIMEOUT = 300
BEDROCK_MAX_ATTEMPTS = 1

# Model settings the manifest may carry through to BedrockModel. Anything else
# in the block is ignored, so a terraform-only key cannot break a request.
PASSTHROUGH_MODEL_SETTINGS = ("temperature", "top_p", "stop_sequences", "additional_request_fields")


class ModelNotConfigured(ValueError):
    """No model id was resolved. A deployment error, reported as a caller-visible one."""


def client_config(
    connect_timeout: int = BEDROCK_CONNECT_TIMEOUT,
    read_timeout: int = BEDROCK_READ_TIMEOUT,
    max_attempts: int = BEDROCK_MAX_ATTEMPTS,
) -> Config:
    """The botocore config every Bedrock call in this platform uses."""
    return Config(
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        retries={"max_attempts": max_attempts, "mode": "standard"},
    )


def build_model(
    model_id: str,
    region: str,
    max_tokens: int,
    connect_timeout: int = BEDROCK_CONNECT_TIMEOUT,
    read_timeout: int = BEDROCK_READ_TIMEOUT,
    max_attempts: int = BEDROCK_MAX_ATTEMPTS,
    model_settings: Optional[Mapping[str, Any]] = None,
) -> BedrockModel:
    """A BedrockModel for one request.

    Raises before any network call when no model is configured, so the caller
    gets the fix in the message instead of a confusing AWS error.
    """
    if not model_id:
        raise ModelNotConfigured(
            "No Bedrock model configured. Set BEDROCK_MODEL_ID or "
            "SONNET5_INFERENCE_PROFILE_ARN."
        )

    extra = {
        key: value
        for key, value in (model_settings or {}).items()
        if key in PASSTHROUGH_MODEL_SETTINGS and value is not None
    }

    return BedrockModel(
        boto_session=boto3.Session(region_name=region),
        boto_client_config=client_config(connect_timeout, read_timeout, max_attempts),
        model_id=model_id,
        max_tokens=max_tokens,
        **extra,
    )


def build_agent(
    model: BedrockModel,
    system_prompt: str,
    agent_name: str,
    session_id: str = "",
) -> Agent:
    """A fresh Strands Agent.

    Deliberately not cached: a new Agent per attempt keeps a repair instruction
    separate from the invalid answer it is repairing, while the trace attribute
    still ties both attempts to one session.
    """
    return Agent(
        model=model,
        system_prompt=system_prompt,
        name=agent_name,
        # The runtime already streams nothing back to the caller; a callback
        # handler would only print the answer twice into CloudWatch.
        callback_handler=None,
        trace_attributes={"session.id": session_id} if session_id else {},
    )


def extract_text(result: Any) -> str:
    """The first non-empty text block of a Strands result.

    A result with only tool or reasoning blocks is a failure, not an empty
    answer: returning "" would let a blank response reach validation and be
    reported as a formatting problem instead of a model problem.
    """
    message = getattr(result, "message", None) or {}
    for item in message.get("content") or []:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            if item["text"].strip():
                return item["text"].strip()
    raise ValueError("Model returned no text.")


def ask(
    model: BedrockModel,
    prompt: str,
    system_prompt: str,
    agent_name: str,
    session_id: str = "",
    max_tokens: int = 0,
) -> str:
    """One model call, returning its text. Truncation is translated, not re-raised."""
    agent = build_agent(model, system_prompt, agent_name, session_id)
    try:
        result = agent(prompt)
    except MaxTokensReachedException as exc:
        # The answer is unusable and a retry with the same budget would be cut
        # off in the same place, so the caller is told what to change.
        raise ValueError(
            f"Model output was cut off at {max_tokens} tokens. The source may be "
            "too large for one request; split it into smaller logical units."
        ) from exc
    return extract_text(result)


__all__ = [
    "BEDROCK_CONNECT_TIMEOUT",
    "BEDROCK_MAX_ATTEMPTS",
    "BEDROCK_READ_TIMEOUT",
    "ModelNotConfigured",
    "PASSTHROUGH_MODEL_SETTINGS",
    "ask",
    "build_agent",
    "build_model",
    "client_config",
    "extract_text",
]
