"""Lambda front door for the AgentCore agent runtimes.

Callers -- API Gateway, Step Functions, a developer with `aws lambda invoke` --
reach the agent through here rather than holding
`bedrock-agentcore:InvokeAgentRuntime` themselves. The handler does four things
and nothing else:

* normalises the two event shapes (direct invoke, API Gateway proxy) into one
  payload;
* settles the session id, deriving a stable one when the caller has none, so
  retries of the same request land in the same AgentCore session;
* invokes the runtime and drains whatever the SDK returns -- bytes, a streaming
  body, or an event stream;
* turns every failure into a clean HTTP-shaped response. Stack traces go to
  CloudWatch, never to the caller: the agent's error messages are written for a
  developer, while a traceback would leak ARNs and module paths.

Deliberately dependency-free beyond boto3. This is a separate deployment
artifact from the agent image, so importing tina_agent_base would mean shipping
the agent's library into the Lambda bundle to re-read three environment
variables.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import uuid
from typing import Any, Mapping, Optional

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())

# --- configuration -------------------------------------------------------------------

AGENT_RUNTIME_ARN = os.environ.get("AGENT_RUNTIME_ARN", "").strip()
AGENT_QUALIFIER = os.environ.get("AGENT_RUNTIME_QUALIFIER", "DEFAULT").strip() or "DEFAULT"
REGION = (
    os.environ.get("AWS_REGION")
    or os.environ.get("AWS_DEFAULT_REGION")
    or "eu-central-1"
)

# The agent enforces its own source limit; this one exists so an accidental
# 10 MB upload is refused here instead of paying for a runtime invocation.
MAX_SOURCE_LENGTH = int(os.environ.get("MAX_SOURCE_LENGTH", "150000"))

# The runtime can spend minutes on one source. A Lambda that gives up first
# would leave the caller with a timeout and the agent still working.
INVOKE_READ_TIMEOUT = int(os.environ.get("INVOKE_READ_TIMEOUT", "870"))
INVOKE_CONNECT_TIMEOUT = 10

# Payload keys that may carry the source, in the agent's own order. Whatever
# arrives is forwarded as `prompt`, which is the key the agent reads last and
# the one the runtime's own console sends.
SOURCE_FIELDS = ("source", "sql", "glue_job", "airflow_job", "prompt")

# AgentCore accepts 33-100 characters of URL-safe text.
SESSION_ID_ALLOWED = re.compile(r"^[A-Za-z0-9_.\-]{1,100}$")
SESSION_PREFIX = os.environ.get("SESSION_PREFIX", "tina-invoker").strip() or "tina-invoker"

_CLIENT: Any = None


class CallerError(ValueError):
    """Something the caller can fix. Reported as 400, with the reason."""


# --- event parsing -------------------------------------------------------------------


def _decode_body(event: Mapping[str, Any]) -> dict:
    """The JSON body of an API Gateway proxy event."""
    body = event.get("body")
    if body is None:
        return {}
    if event.get("isBase64Encoded"):
        try:
            body = base64.b64decode(body).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise CallerError("Request body is not valid base64-encoded UTF-8.") from exc
    if isinstance(body, (dict, list)):
        return body if isinstance(body, dict) else {}
    try:
        parsed = json.loads(body)
    except (TypeError, ValueError) as exc:
        raise CallerError("Request body is not valid JSON.") from exc
    if not isinstance(parsed, dict):
        raise CallerError("Request body must be a JSON object.")
    return parsed


def read_event(event: Any) -> dict:
    """One payload from either event shape.

    API Gateway wraps the caller's JSON in `body`; a direct invoke hands it over
    as-is. Headers are consulted for the session id because an HTTP client
    cannot always add a field to a body it is proxying.
    """
    if not isinstance(event, Mapping):
        raise CallerError("Event must be a JSON object.")

    if "body" in event or "requestContext" in event:
        payload = _decode_body(event)
        headers = {
            str(key).lower(): value
            for key, value in (event.get("headers") or {}).items()
        }
        if not payload.get("runtimeSessionId"):
            header_session = headers.get("x-amzn-bedrock-agentcore-runtime-session-id") or headers.get(
                "x-session-id"
            )
            if header_session:
                payload["runtimeSessionId"] = header_session
        request_id = (event.get("requestContext") or {}).get("requestId")
        if request_id:
            payload.setdefault("_requestId", request_id)
        return payload

    return dict(event)


def read_source(payload: Mapping[str, Any]) -> str:
    """The first field carrying source text."""
    for field in SOURCE_FIELDS:
        value = payload.get(field)
        if isinstance(value, str) and value.strip():
            source = value.strip()
            if len(source) > MAX_SOURCE_LENGTH:
                raise CallerError(
                    f"Source is {len(source)} characters; the maximum is "
                    f"{MAX_SOURCE_LENGTH}. Split it into smaller logical units."
                )
            return source
    raise CallerError(f"Missing source content. Supply one of: {', '.join(SOURCE_FIELDS)}.")


def resolve_session_id(payload: Mapping[str, Any], source: str) -> str:
    """The caller's session id, or a stable one derived from the source.

    Derived rather than random: a retry of the same request then reuses the same
    AgentCore session, so the agent's memory sees one conversation instead of a
    new one per attempt. The hash also keeps the caller's source out of the id.
    """
    supplied = payload.get("runtimeSessionId")
    if isinstance(supplied, str) and SESSION_ID_ALLOWED.match(supplied.strip()):
        candidate = supplied.strip()
        if len(candidate) >= 33:
            return candidate
        # Too short for AgentCore: keep the caller's id visible, pad the rest.
        return f"{candidate}-{uuid.uuid5(uuid.NAMESPACE_OID, candidate).hex}"[:100]

    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:40]
    return f"{SESSION_PREFIX}-{digest}"[:100]


# --- runtime invocation --------------------------------------------------------------


def _client() -> Any:
    """The AgentCore data-plane client, cached across warm invocations."""
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = boto3.client(
            "bedrock-agentcore",
            region_name=REGION,
            config=Config(
                connect_timeout=INVOKE_CONNECT_TIMEOUT,
                read_timeout=INVOKE_READ_TIMEOUT,
                # One attempt: the agent is not idempotent-cheap, and a retry of
                # a minutes-long call would usually outlive the Lambda anyway.
                retries={"max_attempts": 1, "mode": "standard"},
            ),
        )
    return _CLIENT


def _drain(response: Mapping[str, Any]) -> str:
    """The runtime's answer as text, whatever container it arrived in."""
    body = response.get("response")

    if body is None:
        return ""
    if isinstance(body, (bytes, bytearray)):
        return bytes(body).decode("utf-8", errors="replace")
    if isinstance(body, str):
        return body
    if hasattr(body, "read"):
        return body.read().decode("utf-8", errors="replace")

    # An event stream: SSE chunks that have to be concatenated in order.
    chunks: list[str] = []
    try:
        for event in body:
            if isinstance(event, (bytes, bytearray)):
                chunks.append(bytes(event).decode("utf-8", errors="replace"))
                continue
            payload = (event or {}).get("chunk", {}).get("bytes") if isinstance(event, Mapping) else None
            if payload:
                chunks.append(bytes(payload).decode("utf-8", errors="replace"))
    except TypeError:
        logger.warning("Unrecognised response container: %s", type(body).__name__)
        return ""
    return "".join(chunks)


def _as_json(text: str) -> Any:
    """Parse the agent's answer, tolerating SSE framing and plain text."""
    stripped = text.strip()
    if not stripped:
        return {}
    if stripped.startswith("data:"):
        # SSE: the last data line carries the final answer.
        lines = [line[5:].strip() for line in stripped.splitlines() if line.startswith("data:")]
        stripped = lines[-1] if lines else ""
    try:
        return json.loads(stripped)
    except (TypeError, ValueError):
        return {"test_cases_markdown": text.strip()}


def invoke_runtime(source: str, payload: Mapping[str, Any], session_id: str) -> Any:
    """One InvokeAgentRuntime call. Raises ClientError / BotoCoreError on failure."""
    if not AGENT_RUNTIME_ARN:
        raise RuntimeError("AGENT_RUNTIME_ARN is not set on this function.")

    # `prompt` is the agent's own fallback source field, so the same body works
    # against the runtime's console test button.
    body: dict[str, Any] = {"prompt": source, "runtimeSessionId": session_id}
    context = payload.get("context")
    if context is not None:
        body["context"] = context

    response = _client().invoke_agent_runtime(
        agentRuntimeArn=AGENT_RUNTIME_ARN,
        qualifier=AGENT_QUALIFIER,
        runtimeSessionId=session_id,
        contentType="application/json",
        accept="application/json",
        payload=json.dumps(body).encode("utf-8"),
    )
    return _as_json(_drain(response))


# --- responses -----------------------------------------------------------------------


def _respond(status: int, body: Mapping[str, Any], session_id: str = "") -> dict:
    """An API Gateway proxy response that is also readable as a direct result."""
    payload = dict(body)
    if session_id:
        payload.setdefault("runtimeSessionId", session_id)
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
        },
        "isBase64Encoded": False,
        "body": json.dumps(payload),
    }


def _log(event: str, **fields: Any) -> None:
    """One JSON line per event, so CloudWatch Insights can query the fields."""
    logger.info(json.dumps({"event": event, **fields}, default=str))


# --- entrypoint ----------------------------------------------------------------------


def handler(event: Any, context: Optional[Any] = None) -> dict:
    """Lambda entrypoint. Never raises, never returns a stack trace."""
    request_id = getattr(context, "aws_request_id", "") or ""
    session_id = ""

    try:
        payload = read_event(event)
        source = read_source(payload)
        session_id = resolve_session_id(payload, source)
    except CallerError as exc:
        _log("caller_error", request_id=request_id, reason=str(exc))
        return _respond(400, {"error": str(exc)})
    except Exception:  # noqa: BLE001 - a malformed event must not page anyone
        logger.exception("Could not parse the event")
        return _respond(400, {"error": "Request could not be parsed."})

    _log(
        "invoke_start",
        request_id=request_id,
        session_id=session_id,
        source_chars=len(source),
        runtime=AGENT_RUNTIME_ARN.rsplit("/", 1)[-1],
    )

    try:
        result = invoke_runtime(source, payload, session_id)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "ClientError")
        status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") or 502)
        _log("invoke_failed", request_id=request_id, session_id=session_id, code=code, status=status)
        logger.exception("InvokeAgentRuntime failed")
        # The AWS message can name internal resources, so only the code travels.
        return _respond(
            _client_status(code, status),
            {"error": f"The agent runtime rejected the request ({code})."},
            session_id,
        )
    except BotoCoreError:
        _log("invoke_failed", request_id=request_id, session_id=session_id, code="BotoCoreError")
        logger.exception("InvokeAgentRuntime could not be completed")
        return _respond(504, {"error": "The agent runtime did not answer in time."}, session_id)
    except Exception:  # noqa: BLE001 - the caller gets a clean 500, we get the trace
        logger.exception("Unhandled failure invoking the agent runtime")
        return _respond(500, {"error": "The agent could not be invoked."}, session_id)

    if isinstance(result, Mapping) and result.get("error"):
        # The agent's own errors are written for a developer and are safe to
        # pass through; they mean a bad request far more often than a bad agent.
        _log("agent_error", request_id=request_id, session_id=session_id, reason=str(result["error"]))
        return _respond(422, {"error": str(result["error"])}, session_id)

    cards = result.get("test_cases_markdown", "") if isinstance(result, Mapping) else ""
    _log(
        "invoke_done",
        request_id=request_id,
        session_id=session_id,
        result_chars=len(cards),
        fallback=bool(isinstance(result, Mapping) and result.get("fallback_used")),
    )
    body = dict(result) if isinstance(result, Mapping) else {"result": result}
    body.setdefault("status", "success")
    return _respond(200, body, session_id)


def _client_status(code: str, status: int) -> int:
    """Map an AWS error code onto something a caller can act on."""
    if code in ("ValidationException", "InvalidRequestException"):
        return 400
    if code in ("AccessDeniedException", "UnauthorizedException"):
        return 403
    if code in ("ResourceNotFoundException",):
        return 404
    if code in ("ThrottlingException", "TooManyRequestsException", "ServiceQuotaExceededException"):
        return 429
    if code in ("RuntimeClientError",):
        return 502
    return 502 if status < 400 else status


# Some deployments point the handler at `lambda_handler`; both names work.
lambda_handler = handler
