"""Loads the agent definition from agent.yaml."""

from pathlib import Path
from typing import Any, Optional

import yaml

CONFIG_FILENAME = "agent.yaml"


class Pass:
    """One pass of the pipeline: a prompt plus any model overrides."""

    def __init__(self, spec: dict, defaults: dict):
        self.name = spec.get("name", "unnamed")
        self.description = spec.get("description", "")
        self.prompt = spec.get("prompt", "")

        if not self.prompt.strip():
            raise ValueError(f"Pass '{self.name}' has no prompt in {CONFIG_FILENAME}")

        # Pass-level keys win over the top-level bedrock block.
        self.bedrock = {**defaults, **{
            key: value for key, value in spec.items()
            if key not in ("name", "description", "prompt")
        }}

    def render(self, **variables: str) -> str:
        """Fill the prompt template, naming any placeholder we cannot supply."""
        try:
            return self.prompt.format(**variables)
        except KeyError as exc:
            raise ValueError(
                f"Pass '{self.name}' references unknown placeholder {exc} "
                f"in {CONFIG_FILENAME}. Available: {sorted(variables)}"
            ) from exc


class AgentConfig:
    """Parsed agent.yaml."""

    def __init__(self, data: dict, source: Path):
        self.source = source
        self.name = data.get("name", "agent")
        self.description = data.get("description", "")
        self.bedrock: dict[str, Any] = data.get("bedrock") or {}

        passes = data.get("pipeline") or []
        if not passes:
            raise ValueError(f"No 'pipeline' passes defined in {source}")

        self.passes = [Pass(spec, self.bedrock) for spec in passes]


def load(path: Optional[str] = None) -> AgentConfig:
    """Load agent.yaml, defaulting to the one beside this file."""
    config_path = Path(path) if path else Path(__file__).parent / CONFIG_FILENAME

    if not config_path.is_file():
        raise ValueError(f"Agent config not found: {config_path}")

    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"Could not read {config_path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"{config_path} must contain a YAML mapping")

    return AgentConfig(data, config_path)
