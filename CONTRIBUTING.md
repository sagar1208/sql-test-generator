# Contributing

This repository holds a fleet of BI and data-pipeline agents that run on Amazon Bedrock
AgentCore Runtime, the Lambda front door in front of them, the shared Python library they
all import, and the Terraform that deploys them. Read
[`docs/agent-platform-brief.md`](docs/agent-platform-brief.md) before your first change;
[`docs/runbook.md`](docs/runbook.md) covers running and debugging.

---

## Frozen files — read them, never write them

Some files in this repository are **frozen**. They are the design of record, quoted by the
documentation and diffed against by reviewers. Changing one invalidates every reference to
it.

| File | Status |
|---|---|
| [`README.md`](README.md) | Frozen. |
| [`pyproject.toml`](pyproject.toml) | Frozen. |

Treat the frozen list as append-only history. New work goes in new files.

---

## Adding a new agent

Use the scaffolding script. It exists so that the naming, the manifest shape and the
Terraform wiring are identical across the fleet, and so that the second and third agents
exercise the same `tf_modules/agent` interface the first one proved.

```bash
python scripts/new_agent.py --help
python scripts/new_agent.py <agent_name>
```

`<agent_name>` is `snake_case` and is the agent's identity everywhere: the directory under
`src/agents/`, the `name:` in its manifest, the default `AGENT_NAME`, the Terraform root
directory, and the ECR repository. Pick it once and do not rename it — a rename is a runtime
replacement, and callers hold the runtime ARN.

The script scaffolds the shape every agent has:

```
src/agents/<agent_name>/
├── <agent_name>_assistant.py   the agent module: entrypoint, prompts, validation
├── agent.yaml                  the manifest: name, model, limits, prompts
├── Dockerfile                  linux/arm64, python 3.12, port 8080
├── requirements.txt            the agent's own dependencies
└── README.md                   purpose, payload contract, response shapes, env vars
tf_roots/<agent_name>/          one Terraform root, one state file
```

Then, by hand:

1. **Import from `tina_agent_base`; do not re-declare.** `config` for settings and the
   manifest, `runtime` for the model and the Strands agent, `session` for session ids and
   memory. Every invariant in those modules — the 300-second read timeout, botocore retries
   off, a fresh `Agent` per attempt, a session id rejected rather than repaired — is there
   because getting it wrong fails subtly and only under load. A second copy will drift.
2. **Add any new tuning value to `LIMITS` in `tina_agent_base/config.py`**, not as a bare
   `os.environ` read in your agent. `LIMITS` is the single source of truth that
   `scripts/validate_manifests.py` checks manifests against; a limit that is not in the
   registry cannot be validated in CI and will first be discovered by a container that
   refuses to start.
3. **Keep `tina_agent_base.config` dependency-free.** `runtime` and `session` are imported
   lazily on purpose, because the repository scripts import `config` in CI where `boto3`,
   `strands` and `bedrock_agentcore` are absent. Do not add an eager
   `from . import runtime` to `tina_agent_base/__init__.py`.
4. **Fill in the Terraform root.** `tf_roots/<agent_name>/` calls `tf_modules/agent`. Never
   create a fleet-wide resource in an agent root — that belongs in `tf_roots/shared/`, whose
   outputs the agent roots consume. `shared` is applied first.
5. **Write the agent's README before the code is finished.** The payload contract and the
   response shapes are the part callers depend on, and writing them down first is the
   cheapest way to notice that the contract is wrong.
6. **Validate and check** (see below) before opening the PR.

A Terraform root may be reserved before the agent exists. That is deliberate: it settles the naming and
IAM conventions in review rather than in a hurry later.

---

## Manifest conventions — `agent.yaml`

`agent.yaml` is the agent's **manifest**: the runtime configuration Terraform renders and the
image carries. It is read by `tina_agent_base.config.load_manifest`. Four top-level blocks
are understood, and anything else is ignored — so a Terraform-only key cannot break a
request.

```yaml
# The agent's identity. Must match the directory name under src/agents/.
name: sql_test_agent
description: Data-quality test cards from SQL, AWS Glue and Airflow sources.

model:
  # Region and model id are both overridden by the environment when Terraform
  # injects them. Declaring them here keeps a local run honest.
  region: eu-central-1
  model_id: <model-id-or-inference-profile-arn>
  temperature: 0.2

limits:
  # Keys are the snake_case keys of tina_agent_base.config.LIMITS. Anything
  # omitted falls back to the tested in-code default.
  max_source_length: 150000
  max_tokens: 8000
  max_cards: 5

prompts:
  # Overrides for the in-code prompts. An override that has lost a
  # {placeholder} is ignored with a warning, not applied.
  system: |
    ...
  test_case: |
    ...
```

The rules:

- **Resolution is environment → `agent.yaml` → tested in-code default.** The environment
  wins because Terraform injects it per deployment. The manifest comes next so a limit or a
  prompt can change with a `terraform apply` instead of an image rebuild. The in-code default
  is the value the behaviour was tested against and should be identical in every environment
  — it is a default, not a configuration.
- **Declare limits by the `LIMITS` key, in `limits:`.** They must be whole numbers at or
  above the registered minimum. A violation fails `scripts/validate_manifests.py` in CI, and
  if it reaches a container it raises at import so the runtime never becomes healthy.
- **Never put a secret, an account id or a credential in a manifest.** It is committed and it
  is baked into the image. Model ARNs that embed an account id belong in the Terraform
  variable, injected as an environment variable.
- **Prompt overrides must keep every placeholder.** The code interpolates a fixed set —
  `{nonce}`, `{source}`, `{context}`, `{max_cards}`, `{max_words}`, `{out_of_scope}` for the
  test-case prompt. A missing one is caught by `Manifest.prompt` and the built-in prompt is
  used instead, with a warning in the log. Do not rely on that fallback: `validate_manifests`
  is what should catch it.
- **A manifest override does not relax validation.** Rewriting a prompt to stop asking for
  persistent tables does not stop `_validate_cards` rejecting temporary ones. Prompt and
  validator are two halves of one contract; change both, in the same PR, and update
  [`docs/agent-platform-brief.md`](docs/agent-platform-brief.md) §6.
- **Keep the YAML boring.** `config.py` ships a small standard-library reader so it stays
  importable with nothing installed, and that reader implements a subset of YAML: mappings,
  sequences, scalars and block scalars. Anchors, aliases, flow collections and multi-document
  files will read fine in the container and warn on a bare local run. Plain nested mappings
  and `|` block scalars only.

---

## Repository scripts, pre-commit and CI

Two scripts guard the invariants that a reviewer cannot reasonably check by eye. Both are
designed to run with no AWS credentials and no cloud SDKs installed, and both exit non-zero
on failure so they work identically in a git hook and in CI.

```bash
python scripts/validate_manifests.py        # every agent.yaml against LIMITS and the schema
python scripts/check_generated.py           # generated artefacts still match their sources
```

**`validate_manifests.py`** reads every `src/agents/*/agent.yaml` and checks it against
`tina_agent_base.config.LIMITS` and the block conventions above: unknown limit keys, values
below a registered minimum, non-integer limits, a `name` that does not match the directory,
prompt overrides that have dropped a placeholder. It imports `config` — which is exactly why
`config` must stay dependency-free. It catches in CI what would otherwise be caught by a
container that refuses to start.

**`check_generated.py`** verifies that anything derived is still in sync with what it was
derived from, and fails if it is not. It is a *check*, not a formatter: it never rewrites
files, so a CI failure means "regenerate and commit", not "CI will fix it".

### Running them as a pre-commit hook

```bash
cat > .git/hooks/pre-commit <<'HOOK'
#!/usr/bin/env bash
set -euo pipefail
python scripts/validate_manifests.py
python scripts/check_generated.py
HOOK
chmod +x .git/hooks/pre-commit
```

Or, with the `pre-commit` framework, as two `repo: local`, `language: system` hooks with
`pass_filenames: false`.

### In CI

Run both on every pull request, before anything that needs AWS. Suggested order, cheapest
first:

1. `python -m compileall src scripts`
2. `python scripts/validate_manifests.py`
3. `python scripts/check_generated.py`
4. `terraform fmt -check -recursive` and `terraform validate` per root.
5. `docker build --platform linux/arm64` for any agent whose image changed. Verify the
   architecture — AgentCore Runtime accepts `linux/arm64` only, and ECR will happily store a
   wrong-architecture image that fails at deploy time:
   ```bash
   docker image inspect <tag> --format '{{.Os}}/{{.Architecture}}'   # linux/arm64
   ```

Deploys are never part of a pull-request run.

---

## Code style

Match the existing files. `src/agents/sql_test_agent/sql_test_assistant.py` and
`src/agents/agents_base/tina_agent_base/` are the models to copy.

### Module layout

Every module opens with a docstring: a one-line summary, a blank line, then a short paragraph
saying what the module is *for* and what decision it encodes. Not a list of its functions —
the reader can see those.

```python
"""Bedrock model and Strands agent construction, shared by every TINA agent.

One place decides how a model is called: the timeouts, the retry policy, how a
truncated answer is reported, and how text is pulled back out of a Strands
result. An agent that builds its own client would drift from these on the first
copy-paste, and the drift would only show up under load.
"""
```

Then imports — standard library, blank line, third party, blank line, local — each group
alphabetical. Then constants, then classes and functions.

### Section banners

Divide a long module with a banner comment padded with hyphens to **87 characters**:

```python
# --- configuration -------------------------------------------------------------------
```

The label is lower case. The existing set is `configuration`, `prompts`, `request`,
`model call`, `entrypoint`, `limits`, `yaml`, `manifest`, `resolved settings`, `memory`,
`environment readers`. Reuse a name when it fits rather than inventing a synonym; the point
is that the same landmarks appear in every file.

### Comments explain WHY, never WHAT

This is the rule the repository is most consistent about, and the one most worth keeping. A
comment restating the code is noise; a comment recording the reason a line is the way it is
survives the next refactor and prevents it from being "simplified" back into a bug.

Good — every one of these is from the existing code:

```python
# Default only on None: `or ""` would turn 0, [] or False into "" and skip
# the type check.

# Required, with no default: a missing value must fail the request, not
# silently run on a different model.

# A large source can take minutes to answer; botocore's 60 s default read
# timeout would fail it. Strands retries throttling itself, so botocore is told
# not to retry on top of that.

# The reject threshold, counted over the whole card including its field labels,
# so it has to sit above CARD_WORD_TARGET rather than equal it.
```

Bad:

```python
# Set the region
REGION = os.environ.get("AWS_REGION")

# Loop over the cards
for card in cards:
```

Docstrings follow the same rule. One line for the obvious, and a second paragraph only when
there is a reason to record:

```python
def normalise_session_id(value: Any) -> str:
    """A usable session id from caller input, or "" when there is none.

    Rejecting rather than repairing: a session id is a namespace key, so
    silently rewriting one caller's id into another's shape could collide two
    conversations.
    """
```

### Naming and structure

- Module-private helpers take a leading underscore: `_validate_cards`, `_split_cards`,
  `_env_int`. Anything without one is part of the module's contract; add it to `__all__`.
- Constants are `UPPER_SNAKE` at module top, with `_` digit separators on anything over four
  digits: `150_000`, `8_000`, `2_000`.
- Type-hint public functions, including the return. `tuple[str, str]` and `X | None`, not
  `Tuple` and `Optional`, in new agent code.
- Custom exceptions subclass the specific built-in and say why they exist:
  `class InvalidPayload(ValueError): """A caller error, kept distinct so invoke() does not
  also swallow internal bugs."""`

### Error messages name the fix

An error a caller or an operator reads must say what to change. Include the offending value,
the limit, and the action:

```python
raise InvalidPayload(
    f"Source is {len(source)} characters; the maximum is "
    f"{MAX_SOURCE_LENGTH}. Split it into smaller logical units."
)
```

Use `!r` when echoing a value the user supplied, so whitespace and type are visible. Every
one of these strings is quoted in the runbook's troubleshooting table — if you change a
message, update [`docs/runbook.md`](docs/runbook.md) §7 in the same commit.

### Logging

A module-level `logger = logging.getLogger(__name__)`. Never `print`. **Never log the source,
the context, the assembled prompt or the nonce** — character counts only, as the two existing
log lines do:

```python
logger.info("Generating test cases from %d characters of source", len(source))
logger.info("Done: %d characters of test cases", len(cards))
```

Untrusted caller content does not belong in CloudWatch, and the nonce is a security boundary.
`%s` placeholders, not f-strings, in logging calls.

### Layout

- Prose — docstrings, comments, prompt text — wraps at about 79 columns. Code lines may run
  to 96 where breaking them would hurt readability; nothing exceeds 96.
- Four-space indents, no tabs.
- **No emoji**, anywhere: not in code, comments, docstrings, Markdown, commit messages or PR
  descriptions.
- Markdown follows the same width discipline and uses tables for anything with more than two
  dimensions.

### Prompts

Prompt text is code and is reviewed as code.

- Keep the in-code prompt as the default; a manifest override is a deployment-time
  convenience, not the source of truth.
- Interpolate untrusted content with `.format()`, passing it as an **argument** so braces
  inside it stay inert. **Never `.format()` a string that already contains untrusted
  content**, and never build a prompt with an f-string — a `{` in the caller's SQL becomes a
  placeholder. This is why the repair path formats `REPAIR_PROMPT` alone and concatenates it.
- One nonce per request, minted with `secrets.token_hex(8)`, used in **both** the system
  prompt and the user prompt, reused unchanged across the repair round. The full rules are in
  [`docs/agent-platform-brief.md`](docs/agent-platform-brief.md) §7.
- Whenever you change a prompt's output format, change the validator in the same commit.

---

## Branches and pull requests

- **`master`** is the release branch. **`dev`** is the integration branch and the base for
  everything. Branch from `dev`, and target `dev`.
- Branch names: `<type>/<short-description>` in kebab case, where `<type>` is one of `feat`,
  `fix`, `docs`, `refactor`, `chore`, `tf`. For example `feat/new-agent`,
  `fix/session-id-length-cap`, `tf/sql-test-agent-log-retention`.
- One concern per branch. A prompt change, a Terraform change and a refactor are three pull
  requests, because they have three different review audiences and three different rollback
  stories.
- Commit subjects are imperative mood, no trailing period, 72 characters or fewer:
  `Reject session ids over the AgentCore length cap`. The body explains *why*, wrapped at 72.
- Never commit generated Terraform state, `.env`, `.env.local`, `__pycache__`, or a
  `.bedrock_agentcore.yaml` — it records an account id and a runtime ARN.
- Do not force-push a branch that someone has reviewed.

### Pull-request checklist

- [ ] Branched from `dev` and targeting `dev`.
- [ ] No frozen file touched — `git diff --stat` mentions none of the files in the table at
      the top of this document.
- [ ] `python scripts/validate_manifests.py` passes.
- [ ] `python scripts/check_generated.py` passes.
- [ ] New tuning values registered in `LIMITS`, not read directly from `os.environ`.
- [ ] `tina_agent_base.config` still imports with no third-party packages installed.
- [ ] A changed prompt comes with the matching validator change.
- [ ] A changed error message is reflected in [`docs/runbook.md`](docs/runbook.md) §7.
- [ ] A changed payload or response shape is reflected in the agent's own README.
- [ ] A new agent has its Terraform root, its manifest, its Dockerfile and its README.
- [ ] Image builds `linux/arm64` if the image changed.
- [ ] No secret, account id or credential added to a manifest or a Dockerfile.
- [ ] No emoji.

### Review expectations

A reviewer is checking four things beyond correctness:

1. Does a comment exist for every non-obvious decision, and does it say *why*?
2. Is an invariant being re-derived locally that `tina_agent_base` already owns?
3. Would this change alter the card contract without updating the validator and the brief?
4. Could untrusted caller content reach a `.format()` template, a log line, or a prompt
   region outside the nonce tags?
