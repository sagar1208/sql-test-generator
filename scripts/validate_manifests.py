#!/usr/bin/env python3
"""Validate every agent manifest against what the code will actually accept.

An agent.yaml is read twice -- by terraform at apply time and by the agent at
import time -- so a mistake in it has two ways to bite and neither is quick to
diagnose. A wrong limit deploys cleanly and crashes the container's startup; a
wrong platform survives the whole build and fails at CreateAgentRuntime; an
`environment` block that disagrees with the `limits` block runs fine and quietly
ignores the value someone thought they had changed.

Every rule here exists because the failure it prevents is expensive to find later.
The limit minimums are imported from tina_agent_base.config rather than restated,
so this script cannot drift from the code it is checking.

Usage:
    python scripts/validate_manifests.py [path ...]

Exits 0 when every manifest is valid, 1 otherwise, and 2 when it cannot run at all.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
SHARED_LIB = REPO_ROOT / "src" / "agents" / "agents_base"
if str(SHARED_LIB) not in sys.path:
    sys.path.insert(0, str(SHARED_LIB))

try:
    from tina_agent_base import config
except ImportError as exc:  # pragma: no cover - a broken checkout, not a bad manifest
    print(f"error: cannot import tina_agent_base from {SHARED_LIB}: {exc}", file=sys.stderr)
    raise SystemExit(2) from exc

AGENTS_DIR = REPO_ROOT / "src" / "agents"
MANIFEST_GLOB = "*/agent.yaml"

# Blocks every manifest must carry. Anything optional is checked only if present.
REQUIRED_TOP_LEVEL = (
    "name",
    "description",
    "module",
    "platform",
    "runtime",
    "container",
    "resources",
    "timeouts",
    "memory",
    "environment",
    "iam",
)

REQUIRED_ENV = (
    "AWS_REGION",
    "AGENT_NAME",
    "MAX_SOURCE_LENGTH",
    "MAX_TOKENS",
    "MAX_CARDS",
    "MEMORY_ID",
)

MODEL_ENV = ("BEDROCK_MODEL_ID", "SONNET5_INFERENCE_PROFILE_ARN")

# An environment variable that mirrors a limit. Both are read at runtime, the
# environment wins, and a disagreement means someone edited the wrong one.
ENV_TO_LIMIT = {
    "MAX_SOURCE_LENGTH": "max_source_length",
    "MAX_CONTEXT_LENGTH": "max_context_length",
    "MAX_TOKENS": "max_tokens",
    "MAX_CARDS": "max_cards",
    "CARD_WORD_TARGET": "card_word_target",
    "MAX_CARD_WORDS": "max_card_words",
    "MAX_VALIDATION_RETRIES": "max_validation_retries",
    "BEDROCK_CONNECT_TIMEOUT": "connect_timeout",
    "BEDROCK_READ_TIMEOUT": "read_timeout",
    "BEDROCK_MAX_ATTEMPTS": "max_attempts",
}

BOOLEAN_ENV = ("MEMORY_ENABLED",)

# Placeholders the agent formats into each prompt. A missing one is ignored at
# runtime with a warning, which means the override silently does nothing.
REQUIRED_PLACEHOLDERS = {
    "system": ("nonce",),
    "test_case": ("nonce", "source", "context", "max_cards", "max_words", "out_of_scope"),
    "repair": ("feedback", "max_cards", "max_words"),
}

ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
AGENT_NAME = re.compile(r"^[a-z][a-z0-9_]{2,40}$")
IAM_ACTION = re.compile(r"^[a-z0-9-]+:[A-Za-z0-9*]+$")
VALID_PROTOCOLS = ("HTTP", "MCP", "A2A")
VALID_NETWORK_MODES = ("PUBLIC", "VPC")


class Report:
    """Errors for one manifest, each already naming the key it came from."""

    def __init__(self, path: Path):
        self.path = path
        self.errors: list[str] = []

    def fail(self, where: str, message: str) -> None:
        self.errors.append(f"{where}: {message}")

    @property
    def ok(self) -> bool:
        return not self.errors


def _is_int(value: Any) -> bool:
    """Whether this is a whole number. Booleans are ints in Python and are not."""
    return isinstance(value, int) and not isinstance(value, bool)


def _block(report: Report, data: Mapping[str, Any], name: str) -> dict:
    value = data.get(name)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        report.fail(name, f"must be a mapping, got {type(value).__name__}")
        return {}
    return dict(value)


def _check_identity(report: Report, data: Mapping[str, Any], agent_dir: Path) -> None:
    name = data.get("name")
    if not isinstance(name, str) or not AGENT_NAME.match(name or ""):
        report.fail("name", "must be lowercase letters, digits and underscores (AgentCore rejects hyphens)")
    elif name != agent_dir.name:
        report.fail("name", f"is {name!r} but the directory is {agent_dir.name!r}; the deploy keys off the directory")

    description = data.get("description")
    if not isinstance(description, str) or not description.strip():
        report.fail("description", "must be a non-empty string; it is what the AgentCore console shows")

    platform = data.get("platform")
    if platform != "linux/arm64":
        report.fail(
            "platform",
            f"must be 'linux/arm64', got {platform!r}. AgentCore rejects anything else at CreateAgentRuntime",
        )

    module = data.get("module")
    if not isinstance(module, str) or not module.strip():
        report.fail("module", "must name the module holding the BedrockAgentCoreApp")
    elif not (agent_dir / module).is_file():
        report.fail("module", f"names {module!r}, which does not exist in {agent_dir.name}/")
    elif not module.endswith(".py"):
        report.fail("module", f"names {module!r}, which is not a Python module")


def _check_runtime(report: Report, data: Mapping[str, Any], agent_dir: Path) -> None:
    runtime = _block(report, data, "runtime")
    protocol = runtime.get("server_protocol")
    if protocol not in VALID_PROTOCOLS:
        report.fail("runtime.server_protocol", f"must be one of {', '.join(VALID_PROTOCOLS)}, got {protocol!r}")
    if runtime.get("network_mode") not in VALID_NETWORK_MODES:
        report.fail("runtime.network_mode", f"must be one of {', '.join(VALID_NETWORK_MODES)}")
    port = runtime.get("port")
    if protocol == "HTTP" and port != 8080:
        report.fail("runtime.port", f"must be 8080 for the HTTP protocol, got {port!r}")
    if not isinstance(runtime.get("observability"), bool):
        report.fail("runtime.observability", "must be true or false")

    container = _block(report, data, "container")
    repository = container.get("ecr_repository")
    if not isinstance(repository, str) or not re.match(r"^[a-z0-9][a-z0-9._/-]*$", repository or ""):
        report.fail("container.ecr_repository", "must be a lowercase ECR repository name")
    if not isinstance(container.get("image_tag"), str) or not container.get("image_tag"):
        report.fail("container.image_tag", "must be a non-empty string")
    dockerfile = container.get("dockerfile", "Dockerfile")
    if not isinstance(dockerfile, str) or not (agent_dir / dockerfile).is_file():
        report.fail("container.dockerfile", f"names {dockerfile!r}, which does not exist in {agent_dir.name}/")


def _check_sizing(report: Report, data: Mapping[str, Any]) -> None:
    resources = _block(report, data, "resources")
    for key, minimum in (("cpu", 256), ("memory_mib", 512)):
        value = resources.get(key)
        if not _is_int(value):
            report.fail(f"resources.{key}", "must be a whole number")
        elif value < minimum:
            report.fail(f"resources.{key}", f"must be at least {minimum}, got {value}")
    storage = resources.get("ephemeral_storage_mib")
    if storage is not None and (not _is_int(storage) or storage < 512):
        report.fail("resources.ephemeral_storage_mib", "must be a whole number of at least 512")

    timeouts = _block(report, data, "timeouts")
    for key in ("request_seconds", "session_idle_seconds"):
        if not _is_int(timeouts.get(key)):
            report.fail(f"timeouts.{key}", "must be a whole number of seconds")

    request = timeouts.get("request_seconds")
    read = timeouts.get("bedrock_read_seconds")
    if _is_int(request) and _is_int(read) and request < read + 60:
        # Otherwise the caller is handed a timeout while the model is still
        # answering, and the run looks like a runtime fault rather than a slow one.
        report.fail(
            "timeouts.request_seconds",
            f"is {request}s but bedrock_read_seconds is {read}s; leave at least 60s of headroom",
        )
    if _is_int(request) and request > 28_800:
        report.fail("timeouts.request_seconds", "exceeds the AgentCore maximum of 28800s (8 hours)")


def _check_memory(report: Report, data: Mapping[str, Any]) -> None:
    memory = _block(report, data, "memory")
    enabled = memory.get("enabled")
    if not isinstance(enabled, bool):
        report.fail("memory.enabled", "must be true or false")
    if enabled:
        name = memory.get("name")
        if not isinstance(name, str) or not re.match(r"^[a-zA-Z][a-zA-Z0-9_]{0,47}$", name or ""):
            report.fail("memory.name", "must be letters, digits and underscores, starting with a letter")
        days = memory.get("event_expiry_days")
        if not _is_int(days) or days < 1 or days > 365:
            report.fail("memory.event_expiry_days", "must be a whole number between 1 and 365")
        strategies = memory.get("strategies")
        if strategies is not None and not isinstance(strategies, list):
            report.fail("memory.strategies", "must be a list")


def _check_environment(report: Report, data: Mapping[str, Any], limits: Mapping[str, Any]) -> None:
    environment = _block(report, data, "environment")

    for key, value in environment.items():
        if not ENV_NAME.match(str(key)):
            report.fail(f"environment.{key}", "must be UPPER_SNAKE_CASE")
        if not isinstance(value, str):
            # An unquoted 8000 reaches terraform as a number and fails the
            # map(string) type constraint with a message that names the module,
            # not the manifest.
            report.fail(
                f"environment.{key}",
                f"must be a quoted string, got {type(value).__name__}; a container environment carries text",
            )

    for key in REQUIRED_ENV:
        if key not in environment:
            report.fail(f"environment.{key}", "is required and missing")

    for key in MODEL_ENV:
        if key not in environment:
            report.fail(f"environment.{key}", "must be declared, even if empty, so terraform has somewhere to inject")
    if all(str(environment.get(key, "")).strip() for key in MODEL_ENV):
        report.fail(
            "environment",
            f"sets both {' and '.join(MODEL_ENV)}; the agent takes the first and the other is silently ignored",
        )

    for key in BOOLEAN_ENV:
        value = str(environment.get(key, "")).strip().lower()
        if value and value not in ("true", "false", "1", "0", "yes", "no", "on", "off", "enabled", "disabled"):
            report.fail(f"environment.{key}", f"must be a boolean string, got {value!r}")

    for key, limit_key in ENV_TO_LIMIT.items():
        raw = environment.get(key)
        if raw is None:
            continue
        text = str(raw).strip()
        if not re.fullmatch(r"\d+", text):
            report.fail(f"environment.{key}", f"must be a whole number written as a string, got {raw!r}")
            continue
        value = int(text)
        limit = config.LIMITS[limit_key]
        if value < limit.minimum:
            report.fail(
                f"environment.{key}",
                f"is {value} but the code enforces a minimum of {limit.minimum}; the container would fail at startup",
            )
        declared = limits.get(limit_key)
        if _is_int(declared) and declared != value:
            report.fail(
                f"environment.{key}",
                f"is {value} but limits.{limit_key} is {declared}; the environment wins, so the limits block is a lie",
            )


def _check_limits(report: Report, limits: Mapping[str, Any]) -> None:
    for key, value in limits.items():
        limit = config.LIMITS.get(key)
        if limit is None:
            report.fail(f"limits.{key}", f"is not a limit the code reads. Known: {', '.join(sorted(config.LIMITS))}")
            continue
        if not _is_int(value):
            report.fail(f"limits.{key}", f"must be a whole number, got {value!r}")
        elif value < limit.minimum:
            report.fail(f"limits.{key}", f"must be at least {limit.minimum} ({limit.why}), got {value}")

    target = limits.get("card_word_target")
    reject = limits.get("max_card_words")
    if _is_int(target) and _is_int(reject) and reject <= target:
        # The reject threshold counts the whole card, labels included, so a
        # threshold at or below the prose target rejects compliant answers.
        report.fail(
            "limits.max_card_words",
            f"is {reject} but card_word_target is {target}; a card written to spec would be rejected",
        )


def _check_prompts(report: Report, data: Mapping[str, Any]) -> None:
    prompts = _block(report, data, "prompts")
    for key, placeholders in REQUIRED_PLACEHOLDERS.items():
        text = prompts.get(key)
        if text is None:
            # Absent is legal: the agent falls back to its in-code default.
            continue
        if not isinstance(text, str) or not text.strip():
            report.fail(f"prompts.{key}", "must be non-empty text, or absent to use the built-in prompt")
            continue
        missing = [name for name in placeholders if "{" + name + "}" not in text]
        if missing:
            report.fail(
                f"prompts.{key}",
                f"is missing {', '.join('{' + name + '}' for name in missing)}; the agent would ignore this override",
            )

    unknown = set(prompts) - set(REQUIRED_PLACEHOLDERS)
    if unknown:
        report.fail("prompts", f"declares {', '.join(sorted(unknown))}, which the agent does not read")


def _check_iam(report: Report, data: Mapping[str, Any]) -> None:
    iam = _block(report, data, "iam")
    for key in ("bedrock_actions", "memory_actions"):
        actions = iam.get(key)
        if not isinstance(actions, list) or not actions:
            report.fail(f"iam.{key}", "must be a non-empty list of IAM actions")
            continue
        for action in actions:
            if not isinstance(action, str) or not IAM_ACTION.match(action):
                report.fail(f"iam.{key}", f"contains {action!r}, which is not a 'service:Action' string")


def validate(path: Path) -> Report:
    """Every problem with one manifest."""
    report = Report(path)

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        report.fail("file", f"could not be read: {exc}")
        return report

    try:
        data = config.load_yaml(text)
    except Exception as exc:  # noqa: BLE001 - any parse failure is a manifest error
        report.fail("file", f"is not valid YAML: {exc}")
        return report

    if not isinstance(data, Mapping):
        report.fail("file", "must contain a mapping at the top level")
        return report

    for key in REQUIRED_TOP_LEVEL:
        if key not in data:
            report.fail(key, "is a required top-level block and is missing")

    agent_dir = path.parent
    limits = _block(report, data, "limits")

    _check_identity(report, data, agent_dir)
    _check_runtime(report, data, agent_dir)
    _check_sizing(report, data)
    _check_memory(report, data)
    _check_environment(report, data, limits)
    _check_limits(report, limits)
    _check_prompts(report, data)
    _check_iam(report, data)

    return report


def find_manifests(paths: list[str]) -> list[Path]:
    """The manifests to check: the ones named, or every agent's."""
    if paths:
        return [Path(p).resolve() for p in paths]
    return sorted(AGENTS_DIR.glob(MANIFEST_GLOB))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="*", help="manifests to check (default: every src/agents/*/agent.yaml)")
    parser.add_argument("-q", "--quiet", action="store_true", help="print failures only")
    args = parser.parse_args(argv)

    manifests = find_manifests(args.paths)
    if not manifests:
        print(f"error: no manifests found under {AGENTS_DIR}", file=sys.stderr)
        return 2

    failed = 0
    for path in manifests:
        try:
            relative = path.relative_to(REPO_ROOT)
        except ValueError:
            relative = path
        report = validate(path)
        if report.ok:
            if not args.quiet:
                print(f"ok    {relative}")
            continue
        failed += 1
        print(f"FAIL  {relative}")
        for error in report.errors:
            print(f"        {error}")

    checked = len(manifests)
    if failed:
        print(f"\n{failed} of {checked} manifest(s) invalid.", file=sys.stderr)
        return 1
    if not args.quiet:
        print(f"\n{checked} manifest(s) valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
