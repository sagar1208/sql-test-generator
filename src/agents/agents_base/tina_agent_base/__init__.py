"""tina_agent_base: the shared runtime library behind the TINA BI agents.

Three concerns, one per module:

* `config`  -- environment and agent.yaml resolution, pure standard library.
* `runtime` -- Bedrock model and Strands agent construction.
* `session` -- session id handling and AgentCore short-term memory.

`runtime` and `session` are imported lazily. `config` on its own must stay
importable with nothing installed, because the manifest validator and the
scaffolding script read the platform's limits from it in CI, where the Bedrock
and Strands SDKs are not present.
"""

from __future__ import annotations

import importlib
from typing import Any

from . import config
from .config import LIMITS, Manifest, Settings, load_manifest, load_settings

__version__ = "0.1.0"

_LAZY = ("runtime", "session")

__all__ = [
    "LIMITS",
    "Manifest",
    "Settings",
    "__version__",
    "config",
    "load_manifest",
    "load_settings",
    "runtime",
    "session",
]


def __getattr__(name: str) -> Any:
    """Import `runtime` or `session` on first touch (PEP 562)."""
    if name in _LAZY:
        module = importlib.import_module(f"{__name__}.{name}")
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
