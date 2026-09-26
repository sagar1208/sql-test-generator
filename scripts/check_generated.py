#!/usr/bin/env python3
"""Drift check for the generated platform layout.

The repository has a shape that several things depend on at once: terraform reads
each agent's manifest, the Dockerfile copies a fixed set of files, the invoker
Lambda is zipped straight from a directory, and the scaffolding script writes new
agents in the same pattern. A missing or renamed file in any of those breaks a
deploy rather than a test, and usually with an error that names a path nobody
recognises.

So this checks five things:

1. every agent directory carries the files its deploy needs, and the module its
   manifest names;
2. the shared library still exposes the three modules agents import from it;
3. every terraform root has a `versions.tf`, and every module its four files;
4. requirements are consistent -- the same boto3 floor everywhere, and version
   minimums rather than exact pins;
5. the frozen reference files at the repository root are byte-for-byte unchanged.

Every .py file under src/ and scripts/ is also parsed, so a syntax error cannot
reach a container.

Usage:
    python scripts/check_generated.py [--verbose]

Exits 0 when the layout is intact, 1 on drift, 2 when it cannot run at all.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENTS_DIR = REPO_ROOT / "src" / "agents"
SHARED_PACKAGE = AGENTS_DIR / "agents_base" / "tina_agent_base"
LAMBDAS_DIR = REPO_ROOT / "src" / "lambdas"
TF_MODULES = REPO_ROOT / "tf_modules"
TF_ROOTS = REPO_ROOT / "tf_roots"
SCRIPTS = REPO_ROOT / "scripts"

# Directories under src/agents that are not agents.
NOT_AN_AGENT = {"agents_base", "__pycache__"}

# Files every agent needs. The assistant module is taken from the manifest.
AGENT_FILES = ("agent.yaml", "Dockerfile", "requirements.txt", "README.md")

SHARED_MODULES = ("__init__.py", "config.py", "runtime.py", "session.py")

LAMBDA_FILES = {"agent_invoker": ("handler.py",)}

TF_MODULE_FILES = ("main.tf", "variables.tf", "outputs.tf", "README.md")
TF_ROOT_FILES = ("versions.tf", "README.md")

SCRIPT_FILES = ("validate_manifests.py", "check_generated.py", "new_agent.py")

# Packages every agent image must pin, and the floor the whole repository shares.
REQUIRED_PACKAGES = ("bedrock-agentcore", "strands-agents", "boto3", "pyyaml")
BOTO3_FLOOR = "1.43.0"

# The reference implementation and its helpers. They are frozen: the port in
# src/agents/sql_test_agent was made from them, and a change here means the two
# have silently diverged.
#
# A file that has been REMOVED is not drift -- the platform layout supersedes the
# flat CLI, and removing it is a deliberate step. A file that is still present and
# no longer matches is drift, because that is an edit nobody meant to make.
FROZEN_SHA1 = {
    "pyproject.toml": "38350d1ce9802278a07c0d7c1934586c158479ae",
}

REQUIREMENT_LINE = re.compile(r"^(?P<name>[A-Za-z0-9._-]+)\s*(?P<spec>.*)$")


class Findings:
    """Drift and notes, kept apart so a deliberate removal is not an error."""

    def __init__(self) -> None:
        self.drift: list[str] = []
        self.notes: list[str] = []

    def fail(self, message: str) -> None:
        self.drift.append(message)

    def note(self, message: str) -> None:
        self.notes.append(message)

    @property
    def ok(self) -> bool:
        return not self.drift


def _sha1(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _manifest_module(manifest: Path) -> str:
    """The module a manifest names, read without importing a YAML library."""
    for line in manifest.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^module:\s*(\S+)\s*$", line)
        if match:
            return match.group(1).strip("\"'")
    return ""


# --- checks ---------------------------------------------------------------------------


def check_agents(findings: Findings) -> list[Path]:
    """Every agent directory has its deploy files and the module it declares."""
    if not AGENTS_DIR.is_dir():
        findings.fail(f"{_rel(AGENTS_DIR)} is missing")
        return []

    agents = sorted(
        path
        for path in AGENTS_DIR.iterdir()
        if path.is_dir() and path.name not in NOT_AN_AGENT
    )
    if not agents:
        findings.fail(f"{_rel(AGENTS_DIR)} contains no agents")
        return []

    for agent in agents:
        for name in AGENT_FILES:
            if not (agent / name).is_file():
                findings.fail(f"{_rel(agent / name)} is missing")

        manifest = agent / "agent.yaml"
        if not manifest.is_file():
            continue
        module = _manifest_module(manifest)
        if not module:
            findings.fail(f"{_rel(manifest)} declares no `module:` key")
        elif not (agent / module).is_file():
            findings.fail(f"{_rel(manifest)} names module {module!r}, which does not exist")

        # A root that is not reserved must be able to deploy this agent.
        root = TF_ROOTS / agent.name
        if not root.is_dir():
            findings.fail(f"{_rel(root)} is missing; every agent needs a terraform root")

    return agents


def check_shared_library(findings: Findings) -> None:
    """The three modules every agent imports."""
    if not SHARED_PACKAGE.is_dir():
        findings.fail(f"{_rel(SHARED_PACKAGE)} is missing")
        return
    for name in SHARED_MODULES:
        if not (SHARED_PACKAGE / name).is_file():
            findings.fail(f"{_rel(SHARED_PACKAGE / name)} is missing")


def check_lambdas(findings: Findings) -> None:
    for name, files in LAMBDA_FILES.items():
        directory = LAMBDAS_DIR / name
        if not directory.is_dir():
            findings.fail(f"{_rel(directory)} is missing")
            continue
        for filename in files:
            if not (directory / filename).is_file():
                findings.fail(f"{_rel(directory / filename)} is missing")


def check_terraform(findings: Findings) -> None:
    if not TF_MODULES.is_dir():
        findings.fail(f"{_rel(TF_MODULES)} is missing")
    else:
        modules = sorted(p for p in TF_MODULES.iterdir() if p.is_dir())
        if not modules:
            findings.fail(f"{_rel(TF_MODULES)} contains no modules")
        for module in modules:
            for name in TF_MODULE_FILES:
                if not (module / name).is_file():
                    findings.fail(f"{_rel(module / name)} is missing")

    if not TF_ROOTS.is_dir():
        findings.fail(f"{_rel(TF_ROOTS)} is missing")
        return

    roots = sorted(p for p in TF_ROOTS.iterdir() if p.is_dir())
    if not roots:
        findings.fail(f"{_rel(TF_ROOTS)} contains no roots")
    for root in roots:
        for name in TF_ROOT_FILES:
            if not (root / name).is_file():
                # versions.tf is what makes a reserved root init-able at all, and
                # a root without a README leaves nothing explaining why it is empty.
                findings.fail(f"{_rel(root / name)} is missing")
        has_resources = any(
            (root / name).is_file() for name in ("main.tf", "variables.tf", "outputs.tf")
        )
        if not has_resources:
            findings.note(f"{_rel(root)} is reserved (versions.tf and README.md only)")


def check_scripts(findings: Findings) -> None:
    for name in SCRIPT_FILES:
        if not (SCRIPTS / name).is_file():
            findings.fail(f"{_rel(SCRIPTS / name)} is missing")


def _parse_requirements(path: Path) -> dict[str, str]:
    """Package -> specifier, ignoring comments and blank lines."""
    found: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        match = REQUIREMENT_LINE.match(line)
        if match:
            found[match.group("name").lower()] = match.group("spec").strip()
    return found


def _repo_boto3_floor(findings: Findings) -> str:
    """The boto3 floor the repository declares, wherever it still declares it."""
    root_requirements = REPO_ROOT / "requirements.txt"
    if root_requirements.is_file():
        spec = _parse_requirements(root_requirements).get("boto3", "")
        match = re.match(r">=\s*([0-9.]+)$", spec)
        if match:
            return match.group(1)
        findings.fail(f"{_rel(root_requirements)}: boto3 must be pinned as >=<version>, got {spec!r}")
        return BOTO3_FLOOR

    pyproject = REPO_ROOT / "pyproject.toml"
    if pyproject.is_file():
        match = re.search(r"boto3\s*>=\s*([0-9.]+)", pyproject.read_text(encoding="utf-8"))
        if match:
            return match.group(1)

    findings.note("no root requirements.txt or pyproject boto3 pin found; using the built-in floor")
    return BOTO3_FLOOR


def check_requirements(findings: Findings, agents: list[Path]) -> None:
    """Every agent pins the same packages, by minimum version, with one boto3 floor."""
    floor = _repo_boto3_floor(findings)

    for agent in agents:
        path = agent / "requirements.txt"
        if not path.is_file():
            continue
        found = _parse_requirements(path)

        for package in REQUIRED_PACKAGES:
            if package not in found:
                findings.fail(f"{_rel(path)}: {package} is missing")

        for package, spec in found.items():
            if not spec:
                # An unpinned dependency makes two builds of one commit different.
                findings.fail(f"{_rel(path)}: {package} has no version specifier")
            elif spec.startswith("=="):
                findings.fail(
                    f"{_rel(path)}: {package} is pinned with '==' ({spec}); "
                    "this platform pins minimums so a security fix is one rebuild away"
                )
            elif not spec.startswith(">="):
                findings.fail(f"{_rel(path)}: {package} must use '>=', got {spec!r}")

        boto3_spec = found.get("boto3", "")
        match = re.match(r">=\s*([0-9.]+)$", boto3_spec)
        if match and match.group(1) != floor:
            findings.fail(
                f"{_rel(path)}: boto3>={match.group(1)} disagrees with the repository floor "
                f"of {floor}; one of the two is wrong"
            )


def check_frozen(findings: Findings) -> None:
    """The reference files are unchanged, or deliberately gone."""
    for name, expected in sorted(FROZEN_SHA1.items()):
        path = REPO_ROOT / name
        if not path.is_file():
            findings.note(f"{name} is absent (superseded by the platform layout)")
            continue
        actual = _sha1(path)
        if actual != expected:
            findings.fail(
                f"{name} has been modified: sha1 {actual} != {expected}. "
                "It is the frozen reference for the ported agent; restore it with "
                f"`git checkout -- {name}`"
            )


def check_syntax(findings: Findings) -> int:
    """Parse every generated .py file. A syntax error must not reach a container."""
    checked = 0
    for directory in (REPO_ROOT / "src", SCRIPTS):
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            checked += 1
            try:
                compile(path.read_text(encoding="utf-8"), str(path), "exec")
            except SyntaxError as exc:
                findings.fail(f"{_rel(path)}: syntax error on line {exc.lineno}: {exc.msg}")
            except OSError as exc:
                findings.fail(f"{_rel(path)}: could not be read: {exc}")
    return checked


# --- entrypoint -----------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-v", "--verbose", action="store_true", help="print the notes as well as the drift")
    args = parser.parse_args(argv)

    if not REPO_ROOT.is_dir():
        print(f"error: {REPO_ROOT} is not a directory", file=sys.stderr)
        return 2

    findings = Findings()
    agents = check_agents(findings)
    check_shared_library(findings)
    check_lambdas(findings)
    check_terraform(findings)
    check_scripts(findings)
    check_requirements(findings, agents)
    check_frozen(findings)
    modules = check_syntax(findings)

    if args.verbose:
        for note in findings.notes:
            print(f"note  {note}")

    if not findings.ok:
        for message in findings.drift:
            print(f"DRIFT {message}")
        print(f"\n{len(findings.drift)} problem(s) found.", file=sys.stderr)
        return 1

    print(
        f"ok    {len(agents)} agent(s), {modules} python file(s), "
        f"{len(findings.notes)} note(s). No drift."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
