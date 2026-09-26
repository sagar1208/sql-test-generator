"""Session identity and AgentCore short-term memory.

Two jobs that belong together because both are keyed on the same value:

* the session id, which AgentCore routes on, traces are tagged with, and memory
  is filed under -- normalised here so one bad caller cannot poison a trace or a
  memory namespace;
* memory itself, which is optional. It is off unless MEMORY_ID names a store,
  and every call through it is best-effort: a memory outage must never turn a
  perfectly good answer into a failed request.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any, Iterable, NamedTuple, Optional

from . import config

logger = logging.getLogger(__name__)

# AgentCore accepts 33-100 characters of URL-safe text. A generated id is a
# prefix plus a uuid4 hex, which lands comfortably inside that window.
MIN_SESSION_ID_LENGTH = 33
MAX_SESSION_ID_LENGTH = 100
SESSION_ID_ALLOWED = re.compile(r"^[A-Za-z0-9_.\-]+$")

DEFAULT_SESSION_PREFIX = "tina"

# How much history is read back into a prompt, and how much of one turn is kept.
# Memory is a continuity aid, not a second copy of the source, so both are small.
MEMORY_TURN_LIMIT = 3
MEMORY_TURN_MAX_CHARS = 2_000
MEMORY_HISTORY_MAX_CHARS = 6_000


class SessionInfo(NamedTuple):
    """The session id in use, and whether the caller is the one who chose it."""

    id: str
    supplied: bool

    @property
    def echo(self) -> dict:
        """What to mirror back to the caller.

        Only a caller-supplied id is echoed. A generated id is an internal
        correlation key; returning it would invite a caller to reuse it as
        though the platform had promised them a session.
        """
        return {"runtimeSessionId": self.id} if self.supplied else {}


def new_session_id(prefix: str = DEFAULT_SESSION_PREFIX) -> str:
    """A fresh session id that satisfies AgentCore's length and character rules."""
    clean = re.sub(r"[^A-Za-z0-9\-]", "-", prefix).strip("-") or DEFAULT_SESSION_PREFIX
    return f"{clean}-{uuid.uuid4().hex}"[:MAX_SESSION_ID_LENGTH]


def normalise_session_id(value: Any) -> str:
    """A usable session id from caller input, or "" when there is none.

    Rejecting rather than repairing: a session id is a namespace key, so
    silently rewriting one caller's id into another's shape could collide two
    conversations. An unusable id is dropped and a generated one takes over.
    """
    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    if not candidate:
        return ""
    if len(candidate) > MAX_SESSION_ID_LENGTH or not SESSION_ID_ALLOWED.match(candidate):
        logger.warning("Ignoring an unusable runtimeSessionId of %d characters.", len(candidate))
        return ""
    return candidate


def resolve_session(payload: Any, prefix: str = DEFAULT_SESSION_PREFIX) -> SessionInfo:
    """The session for this request: the caller's if usable, otherwise a new one."""
    raw = payload.get("runtimeSessionId") if isinstance(payload, dict) else None
    supplied = normalise_session_id(raw)
    if supplied:
        return SessionInfo(supplied, True)
    return SessionInfo(new_session_id(prefix), False)


# --- memory ------------------------------------------------------------------------------


def memory_id() -> str:
    """The AgentCore Memory store id, or "" when the agent runs without memory."""
    return config.env_str("MEMORY_ID")


def memory_enabled() -> bool:
    """Memory is on only when a store is named and the switch has not been thrown."""
    if not memory_id():
        return False
    return config.env_flag("MEMORY_ENABLED", True)


def _turn_text(message: Any) -> str:
    """One stored message as text, whatever shape the SDK hands back.

    The event payload shape has moved between SDK releases, so every plausible
    shape is accepted rather than pinning one and crashing on the next release.
    """
    if isinstance(message, str):
        return message.strip()
    if isinstance(message, (list, tuple)):
        return " ".join(part for part in (_turn_text(item) for item in message) if part)
    if not isinstance(message, dict):
        return ""

    role = message.get("role") or message.get("Role") or ""
    content = message.get("content", message.get("Content", ""))
    if isinstance(content, dict):
        body = content.get("text") or content.get("Text") or ""
    elif isinstance(content, (list, tuple)):
        body = _turn_text(content)
    else:
        body = content if isinstance(content, str) else ""

    body = (body or message.get("text") or "").strip()
    if not body:
        return ""
    return f"{str(role).lower() or 'turn'}: {body}" if role else body


class SessionMemory:
    """Short-term session memory. A no-op unless MEMORY_ID names a store.

    Nothing here raises. Memory is an enhancement: losing it costs continuity,
    while failing the request costs the answer the caller asked for.
    """

    def __init__(
        self,
        region: str,
        actor_id: str,
        store_id: Optional[str] = None,
        enabled: Optional[bool] = None,
    ):
        self.region = region
        self.actor_id = config.env_str("MEMORY_ACTOR_ID") or actor_id
        self.store_id = store_id if store_id is not None else memory_id()
        if enabled is None:
            try:
                enabled = memory_enabled()
            except ValueError as exc:
                # A malformed MEMORY_ENABLED is a configuration slip, not a
                # reason to fail requests; it fails closed.
                logger.warning("Memory disabled: %s", exc)
                enabled = False
        self.enabled = bool(enabled and self.store_id)
        self._client: Any = None
        self._broken = False

    @property
    def active(self) -> bool:
        """True when a call would actually reach AgentCore Memory."""
        return self.enabled and not self._broken

    def _memory_client(self) -> Any:
        """The SDK client, built on first use.

        Imported lazily so an image without the memory extra, or a local run,
        simply has no memory rather than no agent.
        """
        if self._client is not None or self._broken:
            return self._client
        try:
            from bedrock_agentcore.memory import MemoryClient  # noqa: PLC0415

            self._client = MemoryClient(region_name=self.region)
        except Exception as exc:  # noqa: BLE001 - any failure means "no memory"
            logger.warning("AgentCore Memory unavailable (%s); continuing without it.", exc)
            self._broken = True
            self._client = None
        return self._client

    def recent_turns(
        self,
        session_id: str,
        limit: int = MEMORY_TURN_LIMIT,
        max_chars: int = MEMORY_HISTORY_MAX_CHARS,
    ) -> list[str]:
        """The last few turns of this session, oldest first. [] when memory is off."""
        if not self.active or not session_id:
            return []
        client = self._memory_client()
        if client is None:
            return []

        try:
            turns = self._read(client, session_id, limit)
        except Exception as exc:  # noqa: BLE001 - never fail a request over history
            logger.warning("Could not read session memory (%s); continuing without it.", exc)
            return []

        lines: list[str] = []
        budget = max_chars
        for turn in turns:
            text = _turn_text(turn)
            if not text:
                continue
            text = text[:MEMORY_TURN_MAX_CHARS]
            if len(text) > budget:
                break
            budget -= len(text)
            lines.append(text)
        return lines

    def _read(self, client: Any, session_id: str, limit: int) -> Iterable[Any]:
        """Read through whichever history API this SDK release exposes."""
        if hasattr(client, "get_last_k_turns"):
            return (
                client.get_last_k_turns(
                    memory_id=self.store_id,
                    actor_id=self.actor_id,
                    session_id=session_id,
                    k=limit,
                )
                or []
            )
        if hasattr(client, "list_events"):
            return (
                client.list_events(
                    memory_id=self.store_id,
                    actor_id=self.actor_id,
                    session_id=session_id,
                    max_results=limit,
                )
                or []
            )
        logger.warning("The installed bedrock-agentcore has no memory read API; skipping.")
        return []

    def record_turn(self, session_id: str, request: str, response: str) -> bool:
        """Store one request/response pair. Returns whether it was written."""
        if not self.active or not session_id:
            return False
        client = self._memory_client()
        if client is None:
            return False

        messages = [
            (request[:MEMORY_TURN_MAX_CHARS], "USER"),
            (response[:MEMORY_TURN_MAX_CHARS], "ASSISTANT"),
        ]
        try:
            client.create_event(
                memory_id=self.store_id,
                actor_id=self.actor_id,
                session_id=session_id,
                messages=messages,
            )
        except Exception as exc:  # noqa: BLE001 - the answer is already correct
            logger.warning("Could not write session memory (%s); continuing.", exc)
            return False
        return True


__all__ = [
    "DEFAULT_SESSION_PREFIX",
    "MAX_SESSION_ID_LENGTH",
    "MEMORY_HISTORY_MAX_CHARS",
    "MEMORY_TURN_LIMIT",
    "MEMORY_TURN_MAX_CHARS",
    "MIN_SESSION_ID_LENGTH",
    "SessionInfo",
    "SessionMemory",
    "memory_enabled",
    "memory_id",
    "new_session_id",
    "normalise_session_id",
    "resolve_session",
]
