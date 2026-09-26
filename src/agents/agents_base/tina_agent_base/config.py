"""Shared environment and agent.yaml configuration for the TINA BI agents.

Every agent resolves the same handful of settings: which region to call, which
model to call, and the size limits that stop one request running away. The rules
live here once so that a second agent cannot quietly pick a different default.

Resolution order is environment -> agent.yaml -> the tested in-code default.
The environment wins because terraform injects it per deployment; agent.yaml
comes next so a prompt or a limit can be changed with a terraform apply instead
of an image rebuild; the in-code default is the value the tests were written
against and should be identical in every environment.

Every value is resolved at import time, so a bad setting fails the container's
startup rather than the caller's request.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

logger = logging.getLogger(__name__)

# The manifest lives beside the agent module so one agent cannot read another's.
MANIFEST_FILENAME = "agent.yaml"

# Frankfurt: the only region this platform has Bedrock model access in.
DEFAULT_REGION = "eu-central-1"


# --- environment readers -------------------------------------------------------------


def env_str(name: str) -> str:
    """An environment value with surrounding whitespace removed."""
    return os.environ.get(name, "").strip()


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


# The underscore name is the one agent.py uses; the public alias exists so the
# manifest validator can reuse the same parser instead of writing a second one.
env_int = _env_int


def env_flag(name: str, default: bool) -> bool:
    """A boolean switch. Anything unrecognisable is a configuration error, not a False."""
    raw = env_str(name).lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on", "enabled"):
        return True
    if raw in ("0", "false", "no", "off", "disabled"):
        return False
    raise ValueError(f"{name} must be a boolean, got {raw!r}.")


# --- limits ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Limit:
    """One tuning value: where it comes from, what it defaults to, and its floor."""

    env: str
    default: int
    minimum: int
    why: str = ""


# The single source of truth for the tuning surface. scripts/validate_manifests.py
# imports this so a manifest cannot declare a limit the code would reject at
# startup -- the failure is caught in CI instead of in a deployed container.
LIMITS: dict[str, Limit] = {
    "max_source_length": Limit(
        "MAX_SOURCE_LENGTH", 150_000, 1_000, "~40k tokens of source"
    ),
    "max_context_length": Limit("MAX_CONTEXT_LENGTH", 50_000, 1_000, "caller's own notes"),
    # Headroom, not a target: the prompt caps the answer at max_cards short
    # cards, and a budget this size is what stops a verbose answer being cut off.
    "max_tokens": Limit("MAX_TOKENS", 8_000, 1_000, "model output budget"),
    "max_cards": Limit("MAX_CARDS", 5, 1, "cards per answer"),
    # The prose length the prompt asks for.
    "card_word_target": Limit("CARD_WORD_TARGET", 120, 20, "words the prompt asks for"),
    # The reject threshold, counted over the whole card including its field
    # labels, so it has to sit above card_word_target rather than equal it.
    "max_card_words": Limit("MAX_CARD_WORDS", 200, 50, "words before a card is rejected"),
    "max_validation_retries": Limit("MAX_VALIDATION_RETRIES", 1, 0, "repair round trips"),
    # A large source can take minutes to answer; botocore's 60 s default read
    # timeout would fail it. Strands retries throttling itself, so botocore is
    # told not to retry on top of that.
    "connect_timeout": Limit("BEDROCK_CONNECT_TIMEOUT", 10, 1, "seconds"),
    "read_timeout": Limit("BEDROCK_READ_TIMEOUT", 300, 30, "seconds"),
    "max_attempts": Limit("BEDROCK_MAX_ATTEMPTS", 1, 1, "botocore attempts"),
}


# --- yaml --------------------------------------------------------------------------------


class YamlSubsetError(ValueError):
    """The manifest uses YAML that the built-in fallback reader does not implement."""


_KEY = re.compile(r"""^(?P<key>[A-Za-z_][\w.\-]*|"[^"]+"|'[^']+')\s*:(?:\s(?P<rest>.*)|\s*$)""")
_BLOCK_STYLES = ("|", "|-", "|+", ">", ">-", ">+")


def _strip_comment(raw: str) -> str:
    """Drop a trailing comment. A '#' inside quotes, or glued to a word, is data."""
    out: list[str] = []
    quote = ""
    for char in raw:
        if quote:
            out.append(char)
            if char == quote:
                quote = ""
            continue
        if char in "\"'":
            quote = char
            out.append(char)
            continue
        # YAML only starts a comment when '#' opens a token.
        if char == "#" and (not out or out[-1] in " \t"):
            break
        out.append(char)
    return "".join(out).rstrip()


def _scalar(raw: str) -> Any:
    """A plain YAML scalar. Quoting forces a string, which is how env vars stay strings."""
    text = raw.strip()
    if not text:
        return ""
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    if text[0] in "[{&*!":
        raise YamlSubsetError(f"flow collections and tags are not supported: {text!r}")
    lowered = text.lower()
    if lowered in ("null", "~"):
        return None
    if lowered in ("true", "yes"):
        return True
    if lowered in ("false", "no"):
        return False
    if re.fullmatch(r"[+-]?\d+", text):
        return int(text)
    if re.fullmatch(r"[+-]?(?:\d+\.\d*|\.\d+)", text):
        return float(text)
    return text


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _next_content(lines: list[str], index: int) -> int:
    """The next line carrying something other than whitespace and comments."""
    while index < len(lines):
        if _strip_comment(lines[index]).strip():
            return index
        index += 1
    return len(lines)


def _parse_block_scalar(
    lines: list[str], index: int, parent_indent: int, style: str
) -> tuple[str, int]:
    """A '|' or '>' block. Comments inside it are literal text, so nothing is stripped."""
    body: list[str] = []
    block_indent: Optional[int] = None
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            body.append("")
            index += 1
            continue
        current = _indent_of(line)
        if current <= parent_indent:
            break
        if block_indent is None:
            block_indent = current
        if current < block_indent:
            break
        body.append(line[block_indent:])
        index += 1

    while body and not body[-1]:
        body.pop()

    if style.startswith(">"):
        folded: list[str] = []
        for entry in body:
            if not entry:
                folded.append("\n")
            elif folded and folded[-1] not in ("", "\n"):
                folded.append(" " + entry)
            else:
                folded.append(entry)
        text = "".join(folded)
    else:
        text = "\n".join(body)

    if style.endswith("-"):
        return text, index
    return text + "\n", index


def _parse_collection(lines: list[str], index: int, indent: int) -> tuple[Any, int]:
    """The mapping or sequence whose entries all start at column `indent`."""
    index = _next_content(lines, index)
    if index >= len(lines):
        return {}, index
    first = _strip_comment(lines[index]).strip()
    if first == "-" or first.startswith("- "):
        return _parse_sequence(lines, index, indent)
    return _parse_mapping(lines, index, indent)


def _parse_sequence(lines: list[str], index: int, indent: int) -> tuple[list[Any], int]:
    items: list[Any] = []
    while True:
        index = _next_content(lines, index)
        if index >= len(lines):
            break
        current = _indent_of(lines[index])
        if current < indent:
            break
        if current > indent:
            raise YamlSubsetError(f"unexpected indentation on line {index + 1}")
        content = _strip_comment(lines[index]).strip()
        if not content.startswith("- "):
            raise YamlSubsetError(f"only scalar sequence items are supported (line {index + 1})")
        items.append(_scalar(content[2:]))
        index += 1
    return items, index


def _parse_mapping(lines: list[str], index: int, indent: int) -> tuple[dict, int]:
    result: dict[str, Any] = {}
    while True:
        index = _next_content(lines, index)
        if index >= len(lines):
            break
        current = _indent_of(lines[index])
        if current < indent:
            break
        if current > indent:
            raise YamlSubsetError(f"unexpected indentation on line {index + 1}")

        content = _strip_comment(lines[index]).strip()
        match = _KEY.match(content)
        if not match:
            raise YamlSubsetError(f"expected 'key: value' on line {index + 1}: {content!r}")
        key = match.group("key").strip("\"'")
        rest = (match.group("rest") or "").strip()

        if rest in _BLOCK_STYLES:
            value, index = _parse_block_scalar(lines, index + 1, current, rest)
        elif rest:
            value, index = _scalar(rest), index + 1
        else:
            nested = _next_content(lines, index + 1)
            if nested < len(lines) and _indent_of(lines[nested]) > current:
                value, index = _parse_collection(lines, nested, _indent_of(lines[nested]))
            else:
                value, index = None, index + 1
        result[key] = value
    return result, index


def _minimal_yaml(text: str) -> dict:
    """Read the YAML subset the manifests use: nested mappings, scalars, '|' blocks, lists.

    It exists so the manifest validator and a local run still work without
    PyYAML installed, and it raises rather than guessing on anything else.
    """
    lines = text.splitlines()
    start = _next_content(lines, 0)
    if start >= len(lines):
        return {}
    data, _ = _parse_collection(lines, start, _indent_of(lines[start]))
    if not isinstance(data, dict):
        raise YamlSubsetError("the manifest must be a mapping at the top level")
    return data


def load_yaml(text: str) -> dict:
    """Parse a manifest, preferring PyYAML and degrading to the built-in reader."""
    try:
        import yaml  # noqa: PLC0415  (optional: pinned in the agent image only)
    except ImportError:
        logger.debug("PyYAML is not installed; reading the manifest with the built-in reader.")
        return _minimal_yaml(text)

    data = yaml.safe_load(text)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("the manifest must be a mapping at the top level")
    return data


# --- manifest ----------------------------------------------------------------------------


class Manifest:
    """An agent.yaml, or an empty stand-in when the agent ships without one."""

    def __init__(self, data: Optional[Mapping[str, Any]] = None, path: Optional[Path] = None):
        self.data: dict[str, Any] = dict(data or {})
        self.path = path

    def __bool__(self) -> bool:
        return bool(self.data)

    def __repr__(self) -> str:
        return f"Manifest(path={str(self.path)!r}, keys={sorted(self.data)})"

    def section(self, name: str) -> dict:
        """One top-level block, always a dict so callers need no isinstance dance."""
        value = self.data.get(name)
        return dict(value) if isinstance(value, Mapping) else {}

    def model(self, key: str, default: Any = None) -> Any:
        """A model setting: temperature, region, an explicit model id."""
        return self.section("model").get(key, default)

    def limit(self, key: str) -> Optional[int]:
        """A declared limit, or None when the manifest leaves it to the code default."""
        value = self.section("limits").get(key)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(
                f"{self.path or MANIFEST_FILENAME}: limits.{key} must be a whole number, "
                f"got {value!r}."
            )
        return value

    def prompt(self, key: str, default: str, required: Iterable[str] = ()) -> str:
        """A prompt override, or the in-code default.

        An override that has lost a placeholder would raise KeyError deep inside
        format() on the first request, so it is checked here and ignored with a
        warning: a typo in a yaml edit must not take the agent down.
        """
        value = self.section("prompts").get(key)
        if value is None:
            return default
        if not isinstance(value, str) or not value.strip():
            logger.warning("%s: prompts.%s is not text; using the built-in prompt.", self.path, key)
            return default
        missing = [name for name in required if "{" + name + "}" not in value]
        if missing:
            logger.warning(
                "%s: prompts.%s is missing %s; using the built-in prompt.",
                self.path,
                key,
                ", ".join("{" + name + "}" for name in missing),
            )
            return default
        return value


def manifest_path(module_file: Optional[str] = None) -> Optional[Path]:
    """Where the manifest should be: AGENT_MANIFEST, else beside the agent module."""
    override = env_str("AGENT_MANIFEST")
    if override:
        return Path(override)
    if module_file is None:
        return None
    return Path(module_file).resolve().parent / MANIFEST_FILENAME


def load_manifest(module_file: Optional[str] = None) -> Manifest:
    """The agent's manifest. Absent or unreadable-by-the-fallback is not fatal."""
    path = manifest_path(module_file)
    if path is None or not path.is_file():
        return Manifest(path=path)

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not read %s (%s); using the built-in defaults.", path, exc)
        return Manifest(path=path)

    try:
        return Manifest(load_yaml(text), path=path)
    except YamlSubsetError as exc:
        # PyYAML is missing and the fallback reader met something it does not
        # implement. The image pins PyYAML, so this is a local run: carry on.
        logger.warning("Could not read %s without PyYAML (%s); using built-in defaults.", path, exc)
        return Manifest(path=path)


# --- resolved settings -------------------------------------------------------------------


def resolve_region(manifest: Optional[Manifest] = None) -> str:
    """The Bedrock region. Bedrock access is granted per region, so this matters."""
    region = env_str("AWS_REGION") or env_str("AWS_DEFAULT_REGION")
    if region:
        return region
    if manifest is not None:
        declared = manifest.model("region")
        if isinstance(declared, str) and declared.strip():
            return declared.strip()
    return DEFAULT_REGION


def resolve_model_id(manifest: Optional[Manifest] = None) -> str:
    """The model or inference profile to call.

    Required, with no default: a missing value must fail the request, not
    silently run on a different model. The empty string is returned rather than
    raised so that a health check still passes and the error names the fix.
    """
    for name in ("BEDROCK_MODEL_ID", "SONNET5_INFERENCE_PROFILE_ARN"):
        value = env_str(name)
        if value:
            return value
    if manifest is not None:
        declared = manifest.model("model_id")
        if isinstance(declared, str) and declared.strip():
            return declared.strip()
    return ""


def resolve_limit(key: str, manifest: Optional[Manifest] = None) -> int:
    """One limit, env first, then the manifest, then the tested default."""
    limit = LIMITS[key]
    declared = manifest.limit(key) if manifest is not None else None
    default = limit.default if declared is None else declared
    if declared is not None and declared < limit.minimum:
        raise ValueError(
            f"{manifest.path or MANIFEST_FILENAME}: limits.{key} must be at least "
            f"{limit.minimum}, got {declared}."
        )
    return _env_int(limit.env, default, limit.minimum)


def resolve_agent_name(default: str, manifest: Optional[Manifest] = None) -> str:
    """The name reported to Strands and to traces."""
    name = env_str("AGENT_NAME")
    if name:
        return name
    if manifest is not None:
        declared = manifest.data.get("name")
        if isinstance(declared, str) and declared.strip():
            return declared.strip()
    return default


@dataclass(frozen=True)
class Settings:
    """Everything one agent needs from its environment, resolved once at import."""

    region: str
    model_id: str
    agent_name: str
    max_source_length: int
    max_context_length: int
    max_tokens: int
    max_cards: int
    card_word_target: int
    max_card_words: int
    max_validation_retries: int
    connect_timeout: int
    read_timeout: int
    max_attempts: int
    manifest: Manifest = field(default_factory=Manifest, compare=False, repr=False)

    def describe(self) -> str:
        """A one-line summary safe to log: no ARNs, no credentials."""
        return (
            f"region={self.region} model={'set' if self.model_id else 'MISSING'} "
            f"agent={self.agent_name} max_cards={self.max_cards} "
            f"max_tokens={self.max_tokens} max_source={self.max_source_length}"
        )


def load_settings(module_file: Optional[str] = None, *, agent_name: str = "agent") -> Settings:
    """Resolve every setting for one agent, raising on anything unusable."""
    manifest = load_manifest(module_file)
    limits = {key: resolve_limit(key, manifest) for key in LIMITS}

    # The reject threshold is counted over the whole card, labels included, so a
    # threshold at or below the prose target would reject compliant answers.
    if limits["max_card_words"] <= limits["card_word_target"]:
        raise ValueError(
            f"MAX_CARD_WORDS ({limits['max_card_words']}) must exceed CARD_WORD_TARGET "
            f"({limits['card_word_target']}); otherwise a card written to spec is rejected."
        )

    return Settings(
        region=resolve_region(manifest),
        model_id=resolve_model_id(manifest),
        agent_name=resolve_agent_name(agent_name, manifest),
        manifest=manifest,
        **limits,
    )


__all__ = [
    "DEFAULT_REGION",
    "LIMITS",
    "MANIFEST_FILENAME",
    "Limit",
    "Manifest",
    "Settings",
    "YamlSubsetError",
    "env_flag",
    "env_int",
    "env_str",
    "load_manifest",
    "load_settings",
    "load_yaml",
    "manifest_path",
    "resolve_agent_name",
    "resolve_limit",
    "resolve_model_id",
    "resolve_region",
]
