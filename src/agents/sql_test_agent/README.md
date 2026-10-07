# sql_test_agent

Reads a SQL query, an AWS Glue ETL job or an Airflow DAG and returns plain-English
data-quality test cases — "cards" — that a SQL developer can implement **without reading the
source**.

One Bedrock call per request, one optional repair call, and a deterministic validator that
refuses to hand back a card the developer could not actually run.

| | |
|---|---|
| Module | `sql_test_assistant.py` |
| Manifest | `agent.yaml` |
| Shared library | [`tina_agent_base`](../agents_base/tina_agent_base/) |
| Runtime | Amazon Bedrock AgentCore, `linux/arm64`, port 8080 |
| Framework | Strands, on Amazon Bedrock |
| Terraform root | `tf_roots/sql_test_agent/` |
| Front door | `src/lambdas/agent_invoker/handler.py` |

Platform architecture: [`docs/agent-platform-brief.md`](../../../docs/agent-platform-brief.md).
Operations: [`docs/runbook.md`](../../../docs/runbook.md).

---

## What it is for

A data-quality test written by a junior developer checks nulls and row counts. Those defects
announce themselves. The defects that matter are the ones that put wrong numbers in a finance
report and go unnoticed for two weeks: a join that fans out, a `GROUP BY` at the wrong grain,
an `INSERT` with no preceding `DELETE` so every rerun appends the dataset again, a cast that
silently truncates, a rolling date window that is off by one.

This agent is prompted to look for exactly those, and constrained so that what it produces is
implementable:

- **No executable code.** Assertions are described in prose. Naming a table or a column is
  fine; composing a statement is not.
- **Persistent objects only.** `Source table` and `Target table` may name only objects that
  exist before and after the run — never a temp table, a `#table`, a CTE, a Spark temp view, a
  `DataFrame` or a `DynamicFrame`. The developer opening a SQL client tomorrow cannot query
  any of those.
- **Nothing invented.** Every table a card names must occur literally in the supplied source,
  spelled exactly as the source spells it.
- **Bounded.** At most `MAX_CARDS` cards, each under `MAX_CARD_WORDS` words, and fewer cards
  when the source carries less risk. A pipeline with one real weakness gets one card.

The first three are enforced mechanically, not merely requested. See
[Validation](#validation).

---

## Payload contract

### The source — five accepted fields

```python
SOURCE_FIELDS = ("source", "sql", "glue_job", "airflow_job", "prompt")
```

| Field | Intended for |
|---|---|
| `source` | Anything: the generic, explicit field. |
| `sql` | A SQL query or script. Used by the worked example below. |
| `glue_job` | An AWS Glue ETL job — the PySpark module. |
| `airflow_job` | An Airflow DAG module. |
| `prompt` | The generic field the `agent_invoker` Lambda sends. |

All five behave identically — the names document caller intent, they do not change the
prompt. The rules:

- **Exactly one is used: the first in that tuple order holding a non-empty string.** The
  order in the tuple is the precedence, **not** the order of keys in your JSON. A payload with
  both `sql` and `prompt` is answered from `sql`, and `prompt` is ignored entirely. `prompt`
  is last precisely because it is the generic field the Lambda fills in, so an explicit field
  always wins over it.
- A value that is not a string is skipped as though the key were absent. A whitespace-only
  string counts as empty.
- The winning value is `strip()`ped, then length-checked against `MAX_SOURCE_LENGTH`
  (150000 characters, about 40k tokens).
- None of the five present, or all empty:
  `Missing source content. Supply one of: source, sql, glue_job, airflow_job, prompt.`

### `context` — optional business context

A free-text string: what the pipeline is for, what the data should look like, which rules the
tests should assume. It is interpolated into the prompt inside its own nonce-tagged region and
is treated as data, never as instruction.

- Optional. Omit it, or send `null`, and the prompt records `none supplied`.
- **Defaulted only when it is `None`.** Any other non-string value is an error —
  `Field 'context' must be a string` — including `0`, `false`, `[]` and `{}`.
- Capped at `MAX_CONTEXT_LENGTH` (50000 characters):
  `Context is 61200 characters; the maximum is 50000.`
- Stripped before use.

> **The `or ""` trap.** The natural way to write this is
> `context = payload.get("context") or ""`, and it is wrong. `or` treats `0`, `False`, `[]`
> and `{}` as absent, so all four would silently become `""` and skip the type check — a
> caller that accidentally sent `"context": 0` would get an answer generated with no context
> at all and no indication anything was dropped. The code therefore tests `is None`
> explicitly, then type-checks, then length-checks, **in that order**. Reversing any two of
> those steps reintroduces the bug: a length check on a non-string raises `TypeError` instead
> of the caller-facing message.

### `runtimeSessionId` — optional session identity

Correlates a request with a trace and, when memory is enabled, with earlier turns in the same
conversation.

- Optional. Omitted, or unusable, and the agent generates one (`sql-test-<uuid4hex>`).
- Must be a string of URL-safe characters — `A-Z a-z 0-9 _ . -` — at most 100 characters.
  Anything longer, or containing anything else, is **dropped and replaced**, with a warning in
  the log. It is rejected rather than repaired: a session id is a namespace key, and silently
  rewriting one caller's id into another's shape could collide two conversations in a memory
  store.
- AgentCore itself wants **33 to 100** characters. The agent's own normaliser enforces only
  the 100-character cap and the character set, so a shorter id passes the agent and may still
  be rejected by the runtime in front of it. Send at least 33 characters.
- **It is echoed back only when you supplied it.** A generated id is an internal correlation
  key; returning it would imply the platform had promised you a session it has not. So
  `runtimeSessionId` appears in the response if and only if it was in the request.

### A complete request

```json
{
  "sql": "INSERT INTO finance.fct_ksb1 SELECT c.* FROM raw.ksb1 c JOIN dim.cost_center d ON c.kostl = d.kostl",
  "context": "Monthly finance close. Reruns are common and the target is never truncated first.",
  "runtimeSessionId": "finance-close-2026-09-fctksb1-run-000001"
}
```

Unknown keys are ignored.

---

## Response contract

Both shapes are returned with HTTP 200. **`status` in the body is the only indicator of
success** — a failed generation is not an HTTP error.

### Success

```json
{
  "status": "success",
  "test_cases_markdown": "TC-001 - Rerun appends the whole month again\n\nCategory     : ...",
  "runtimeSessionId": "finance-close-2026-09-fctksb1-run-000001"
}
```

| Key | Always present | Meaning |
|---|---|---|
| `status` | yes | `"success"` |
| `test_cases_markdown` | yes | The cards, as one markdown string. May instead be the out-of-scope sentinel. |
| `runtimeSessionId` | only if you sent one | Echo of your session id. |
| `fallback_used` | only when partial | `true` — validation failed after the repair round and only the individually valid cards were returned. |
| `validation_errors` | only with `fallback_used` | The defects that caused cards to be dropped. |

### Success, but partial — fallback generation

```json
{
  "status": "success",
  "test_cases_markdown": "TC-001 - ...\n\nTC-002 - ...",
  "fallback_used": true,
  "validation_errors": ["TC-003 is missing 'Pass criteria'.",
                        "TC-004 names a temporary object in 'Source table'."]
}
```

The cards you receive are fully valid — they passed the same per-card checks as any other
answer. What you are **not** getting is a complete set. One bad card out of four costs you
that card, not the whole answer. Surface `fallback_used` downstream rather than treating a
short answer as a complete one.

### Success, but out of scope

```json
{
  "status": "success",
  "test_cases_markdown": "OUT_OF_SCOPE: this input is not a recognizable SQL query, AWS Glue ETL job, or Airflow DAG."
}
```

The model judged the input not to be SQL, a Glue job or an Airflow DAG. This is a **success**,
and a caller that assumes every successful response is a set of cards will store that sentence
as a test plan. Check for the `OUT_OF_SCOPE:` prefix. In practice it usually means a truncated
file, a README, or a YAML config was sent by mistake.

### Error

```json
{
  "status": "error",
  "error": "Source is 183492 characters; the maximum is 150000. Split it into smaller logical units.",
  "runtimeSessionId": "finance-close-2026-09-fctksb1-run-000001"
}
```

`invoke()` never raises: every failure, caller error or internal, arrives as an `error` string.
`handle()` chooses the envelope purely on whether the `error` key is present. Every message you
can receive is tabulated in [`docs/runbook.md`](../../../docs/runbook.md) §7.

---

## The card format

The prompt asks for this exact layout. `TC-<nnn>` numbering starts at `001`.

```
TC-<nnn> - <short title describing the production risk>

Category     : <one of the categories below>
Priority     : <High or Medium or Low>
Source table : <persistent table(s) read>
Target table : <persistent table(s) written, or "query output">
Key columns  : <business key columns>

What to test
  <Which columns, which tables, and which condition. Maximum 2 sentences.>

Pass criteria
  <One measurable outcome: zero rows, counts equal, max group size 1, set
  difference empty. One line.>

Failure means
  <What breaks in production and why it matters. One sentence.>
```

Cards are separated by a blank line.

### Every section

| Section | Required | Notes |
|---|---|---|
| `TC-<nnn>` title line | yes | `TC-001 - Title`. Markdown decoration is tolerated: `## TC-001: Title`, `> TC-7 — Title` and `TC-12 Title` all parse. Ids must be unique — and `TC-001` and `TC-1` are the **same** id. |
| `Category` | no, not validated | One of: schema/structure, not-null, uniqueness/key, referential integrity, join correctness, filter correctness, aggregation correctness, deduplication, date/window logic, business rule, reconciliation, incremental-load/idempotency, operational. |
| `Priority` | no, not validated | `High` — wrong data reaches the target silently. `Medium` — data missing but detectable within one run cycle. `Low` — operational; the job fails loudly and data is correct or absent. |
| `Source table` | **yes** | Persistent tables read, comma-separated. Every name is checked. `query output` is **not** accepted here. |
| `Target table` | **yes** | Persistent tables written, comma-separated, or the literal `query output` when the source writes nothing. |
| `Key columns` | **yes** | The business key. Free text; not checked against the source. |
| `What to test` | **yes** | Heading alone on its line, body **indented**. |
| `Pass criteria` | **yes** | Heading alone on its line, body **indented**. One measurable outcome. |
| `Failure means` | **yes** | Heading alone on its line, body **indented**. |

### Two section grammars — and why the indentation matters

The three field-style sections (`Source table`, `Target table`, `Key columns`) are
`Label : value` on one line. The three prose sections (`What to test`, `Pass criteria`,
`Failure means`) are a **bare heading on its own line** followed by an **indented** body.

They are not interchangeable. For the prose sections:

| Written as | Parsed value | Result |
|---|---|---|
| heading line, then a body indented by a space or tab | the body | valid |
| heading line, then a flush-left body | `""` | `TC-001 is missing 'What to test'.` |
| `What to test : some prose` on one line | `""` | `TC-001 is missing 'What to test'.` |

Body collection stops at the first blank line or the first line starting in column one. So if
you are looking at output that plainly contains a `What to test` section and the error says it
is missing, the indentation is the answer. This is also the most common reason the repair round
is entered at all.

---

## Validation

Every answer is checked before it is returned. A card that fails is not returned.

### Per-answer

| Rule | Error |
|---|---|
| At least one `TC-<nnn>` card, unless the answer is exactly the out-of-scope sentinel | `No test cards were found.` |
| At most `MAX_CARDS` cards | `The answer contains 7 cards; maximum is 5.` |

### Per-card

| Rule | Error |
|---|---|
| Starts with `TC-<number>` | `A card does not start with TC-<number>.` |
| Unique id | `Duplicate card id TC-001.` |
| At most `MAX_CARD_WORDS` words, counted over the whole card including its labels | `TC-001 is longer than 200 words.` |
| All six required sections present and non-empty | `TC-001 is missing 'Pass criteria'.` |
| No temporary object in `Source table` / `Target table` | `TC-001 names a temporary object in 'Source table'.` |
| Every table name occurs literally in the source | `TC-001 names 'orders', which does not appear in the source.` |

### The persistent-table rule in detail

An object counts as temporary if the source shows it as any of:

- `CREATE [OR REPLACE] TEMP[ORARY] TABLE|VIEW <name>`;
- a CTE — the `<name>` in `WITH <name> AS (` or a following `, <name> AS (`;
- anything written `#<name>`.

Why: **the card must be runnable by a developer after the job has finished.** A temp table's
session is gone, a CTE is scoped to one statement, a Spark temp view dies with its
`SparkSession`, and a `DataFrame` is an in-memory handle inside one job run. A card asserting
on one of those cannot be run at all, and a developer who discovers that after twenty minutes
stops trusting the rest of the output. When the risk genuinely sits in a temporary step, the
prompt instructs the model to explain it in `What to test` and assert on the **persistent
table that step finally feeds**.

### Exact spelling, including schema qualification

The occurrence check is `(?<![\w.])<name>(?![\w.])`, case-insensitive. There is no fuzzy
matching and no schema awareness:

| Source says | Card says | Result |
|---|---|---|
| `FROM raw.orders` | `raw.orders` | accepted |
| `FROM raw.orders` | `orders` | rejected |
| `FROM orders` | `raw.orders` | rejected |

The lookarounds also stop `orders` matching `orders_staging` or `db.orders_v2`. A card that
invents a plausible neighbouring name is worse than no card, because it looks runnable and is
not.

Two practical consequences: the field takes **names only** — `raw.orders (fact feed)` is read
as the name `raw.orders (fact feed` and rejected — and a `#word` Python comment in a Glue job,
such as `#TODO`, registers `todo` as a temporary object, which is harmless unless a real table
shares the name.

### The repair round and the fallback

1. One model call produces an answer.
2. If validation finds nothing wrong, it is returned.
3. Otherwise, **one** repair call (`MAX_VALIDATION_RETRIES`, default `1`) is made. The repair
   prompt carries the validator's own error list, so the model is told exactly which card broke
   which rule.
4. If validation still fails, the individually valid cards from the better of the two attempts
   are returned with `fallback_used: true`.
5. Only if nothing at all validates does the request fail with
   `Generated test cases failed validation: <the defect list>`.

A request therefore costs at most two model calls.

---

## A worked example

Input:

```json
{
  "sql": "SELECT u.user_id, u.email, COUNT(o.order_id) as order_count, SUM(o.amount) as total_spent, CASE WHEN SUM(o.amount) > 1000 THEN 'high_value' WHEN SUM(o.amount) > 100 THEN 'medium_value' ELSE 'low_value' END as customer_segment FROM users u LEFT JOIN orders o ON u.user_id = o.user_id WHERE u.created_at >= '2024-01-01' AND o.order_date IS NOT NULL GROUP BY u.user_id, u.email HAVING COUNT(o.order_id) > 0",
  "context": "This query segments customers by their lifetime spending. It pulls from a Salesforce user table and a data warehouse orders table. Results feed into a marketing campaign targeting system. Customers with NULL order_date values should be excluded, and we expect at least 80% of active users to have at least one order."
}
```

Output — `test_cases_markdown`, with the cards a validating answer produces. The query writes
nothing, so every `Target table` is `query output`; the only persistent objects named are
`users` and `orders`, both of which occur literally in the source.

```
TC-001 - order_date filter turns the LEFT JOIN into an inner join

Category     : filter correctness
Priority     : High
Source table : users, orders
Target table : query output
Key columns  : user_id

What to test
  Compare the distinct user_id count in the result against the distinct
  user_id count in users for created_at on or after 2024-01-01. The
  order_date IS NOT NULL predicate sits in WHERE, so users with no orders
  row are removed before the LEFT JOIN can preserve them.

Pass criteria
  Set difference between the two user_id sets is empty, or the exclusion is
  confirmed as intended by the campaign owner.

Failure means
  Newly acquired users who have not ordered yet never enter the campaign
  system, and the 80 percent coverage expectation is measured against a
  population that already excludes them.

TC-002 - Segment boundaries at exactly 100 and exactly 1000

Category     : business rule
Priority     : Medium
Source table : orders
Target table : query output
Key columns  : user_id

What to test
  For users whose total_spent equals exactly 1000 or exactly 100, check
  which customer_segment the CASE expression assigns.

Pass criteria
  Every boundary total lands in the segment the campaign owner has
  confirmed; zero rows disagree.

Failure means
  Customers at the threshold are targeted with the wrong campaign tier and
  the segment counts reported to marketing are wrong by a small, invisible
  amount.

TC-003 - NULL amount collapses a customer into low_value

Category     : not-null
Priority     : Medium
Source table : orders
Target table : query output
Key columns  : user_id, order_id

What to test
  Count orders rows where amount is NULL for the user_id values the query
  returns. SUM ignores NULLs, so a user whose every order has a NULL amount
  totals NULL and falls through to the ELSE branch.

Pass criteria
  Zero orders rows with a NULL amount for users in scope, or a confirmed
  rule for how such users must be segmented.

Failure means
  Customers with unpriced orders are marketed to as low value regardless of
  their real lifetime spend.
```

Three cards, not five, because the query has three real weaknesses — the prompt forbids
padding the count. Note what the cards do *not* do: they name no column the query does not
contain, they propose no threshold the context did not supply (both defer to "confirmed by the
campaign owner"), and they contain no SQL.

A Glue-flavoured example source ships alongside this README as
`tmnl-tina-glue-job-if-finance-fctksb1.py`.

---

## Configuration

Resolution is **environment → `agent.yaml` → tested in-code default**. The environment wins
because Terraform injects it per deployment; `agent.yaml` comes next so a limit or a prompt
can change with a `terraform apply` instead of an image rebuild.

### Identity and location

| Variable | Required | Default | Notes |
|---|---|---|---|
| `BEDROCK_MODEL_ID` | one of the two | none | Model id, inference-profile id, or profile ARN. Checked at request time, not at startup: unset means the container is healthy and every request returns `No Bedrock model configured.` Wins over the ARN variable. |
| `SONNET5_INFERENCE_PROFILE_ARN` | one of the two | none | What `tf_roots/sql_test_agent` wires from the shared inference profile. |
| `AWS_REGION` | no | `eu-central-1` | Bedrock region. **Model access is per region.** Set automatically inside Runtime and Lambda. |
| `AWS_DEFAULT_REGION` | no | — | Used only when `AWS_REGION` is empty. |
| `AGENT_NAME` | no | `sql_test_agent` | Reported to Strands and to traces. |
| `AGENT_MANIFEST` | no | `agent.yaml` beside the module | Path override, for trying a manifest without editing the committed one. |

### Limits

Each is range-checked **at import**, so a bad value stops the container becoming healthy rather
than corrupting one request. `agent.yaml` declares the same values under `limits:` with the
snake_case key shown.

| Variable | `agent.yaml` key | Default | Minimum | Purpose |
|---|---|---|---|---|
| `MAX_SOURCE_LENGTH` | `max_source_length` | `150000` | `1000` | Character cap on the source, about 40k tokens. Rejected before any model call. |
| `MAX_CONTEXT_LENGTH` | `max_context_length` | `50000` | `1000` | Character cap on `context`. |
| `MAX_TOKENS` | `max_tokens` | `8000` | `1000` | Output-token budget. Headroom, not a target. |
| `MAX_CARDS` | `max_cards` | `5` | `1` | Fed to the prompt **and** enforced by the validator. |
| `CARD_WORD_TARGET` | `card_word_target` | `120` | `20` | The card length the prompt asks for. |
| `MAX_CARD_WORDS` | `max_card_words` | `200` | `50` | The reject threshold, counted over the whole card. **Must exceed `CARD_WORD_TARGET`** or the agent refuses to start — a card written exactly to spec would otherwise be rejected. |
| `MAX_VALIDATION_RETRIES` | `max_validation_retries` | `1` | `0` | Repair rounds. `1` caps a request at two model calls; `0` disables repair. |
| `BEDROCK_CONNECT_TIMEOUT` | `connect_timeout` | `10` | `1` | Seconds to connect. |
| `BEDROCK_READ_TIMEOUT` | `read_timeout` | `300` | `30` | Seconds to wait for the answer. Every caller timeout in front of the agent must exceed this. |
| `BEDROCK_MAX_ATTEMPTS` | `max_attempts` | `1` | `1` | botocore attempts. Strands retries throttling itself, so botocore must not retry on top — hence a floor of `1`. |

### Memory

| Variable | Default | Notes |
|---|---|---|
| `MEMORY_ID` | empty | AgentCore Memory store id. **Memory is off while this is empty**, which is a supported mode, not a failure — it is what lets a local run work with nothing but Bedrock. |
| `MEMORY_ENABLED` | `true` (inert without `MEMORY_ID`) | Kill switch. Accepts `1/true/yes/on/enabled` and `0/false/no/off/disabled`; anything else raises at startup rather than being read as off. |
| `MEMORY_ACTOR_ID` | `AGENT_NAME` | Actor id recorded against stored turns. |

When memory is active, up to 3 earlier turns (2000 characters each, 6000 total) are injected as
history — **inside nonce tags, exactly like the source**, because an earlier turn is as
untrusted as this one. The write happens after the answer is in hand and is best-effort: a
memory outage never turns a good answer into a failed request.

### Prompts

`agent.yaml` carries `prompts.system`, `prompts.test_case` and `prompts.repair`, byte-identical
to the in-code defaults, so a prompt fix ships as a `terraform apply`. An override that has
dropped a required `{placeholder}` is **ignored with a warning** rather than failing every
request; required placeholders are `{nonce}` for the system prompt,
`{nonce} {source} {context} {max_cards} {max_words} {out_of_scope}` for the test-case prompt,
and `{feedback} {max_cards} {max_words}` for the repair prompt.

Rewriting a prompt does not relax validation. Removing the persistent-table instruction does
not stop the validator rejecting temporary objects — the prompt and the validator are two
halves of one contract.

---

## Running it

### Locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r src/agents/sql_test_agent/requirements.txt

export BEDROCK_MODEL_ID=<model-id-or-inference-profile-arn>
export AWS_REGION=eu-central-1

python src/agents/sql_test_agent/sql_test_assistant.py
```

No `PYTHONPATH` needed: the module inserts `src/agents/agents_base` into `sys.path` itself when
that directory exists, which is how the identical file runs in a shell, in a test harness and
in the container.

Then, from another shell:

```bash
curl -s localhost:8080/ping

curl -s -X POST localhost:8080/invocations \
  -H 'content-type: application/json' \
  -d '{"sql": "SELECT order_id, amount FROM orders WHERE order_date IS NOT NULL", "context": "daily orders load"}' | python3 -m json.tool
```

### In a container

Build from the **repository root** — the image needs the shared library, which lives outside
this directory:

```bash
docker build --platform linux/arm64 \
  -f src/agents/sql_test_agent/Dockerfile \
  -t sql_test_agent:local .

docker image inspect sql_test_agent:local --format '{{.Os}}/{{.Architecture}}'   # linux/arm64

docker run --rm -p 8080:8080 \
  -e BEDROCK_MODEL_ID=<model-id-or-arn> \
  -e AWS_REGION=eu-central-1 \
  -v ~/.aws:/root/.aws:ro \
  sql_test_agent:local
```

`linux/arm64` is not a preference — AgentCore Runtime accepts nothing else, and ECR will store
a wrong-architecture image happily, so the failure appears only at deploy time. Check the
architecture before every push.

### Deployed

Through the Lambda front door:

```bash
aws lambda invoke \
  --function-name <agent-invoker-function-name> \
  --cli-binary-format raw-in-base64-out \
  --payload '{"prompt": "SELECT ...", "context": "...", "runtimeSessionId": "finance-close-2026-09-fctksb1-run-000001"}' \
  --region eu-central-1 \
  out.json && python3 -m json.tool < out.json
```

Directly against the runtime, for debugging:

```python
import boto3, json

client = boto3.client("bedrock-agentcore", region_name="eu-central-1")
response = client.invoke_agent_runtime(
    agentRuntimeArn="arn:aws:bedrock-agentcore:eu-central-1:<ACCOUNT_ID>:runtime/<RUNTIME_ID>",
    runtimeSessionId="debug-session-00000000000000000000001",
    payload=json.dumps({"sql": "SELECT 1"}),
)
print(json.loads(response["response"].read()))
```

Deploy with `terraform -chdir=tf_roots/sql_test_agent apply`, after `tf_roots/shared`. The full
sequence, log locations and every error message are in
[`docs/runbook.md`](../../../docs/runbook.md).


TC-002 - Bridge join collapses customer mapping via MAX and nullable join key\
\
Category     : join correctness\
Priority     : High\
Source table : rs_qualified_db.kpn_wba_invoicelines, if_finance.fct_kpn_wba_vispbill_bridge\
Target table : if_finance.fct_kpn_penalty_invoice\
Key columns  : technical_service_id, spo_wbaservicegroup, spo_ispcustomerid\
\
What to test\
Check whether multiple spo_ispcustomerid values exist per spo_wbaservicegroup (the MAX() silently picks one), and verify rows where technical_service_id is NULL or has no bridge match, since the LEFT JOIN will produce NULL spo_ispcustomerid for those.\
\
Pass criteria\
Each technical_service_id maps to a confirmed single valid spo_ispcustomerid, and the count of NULL spo_ispcustomerid rows in the target matches the expected count of unmatched technical_service_id values (confirm expected count with business owner).\
\
Failure means\
Invoice lines get silently attributed to the wrong customer, corrupting downstream financial attribution without any error.\


"TC-001 - Full table delete without transactional guarantee on rerun\
\
Category     : operational\
Priority     : High\
Source table : rs_qualified_db.kpn_wba_invoicelines, if_finance.fct_kpn_wba_vispbill_bridge\
Target table : if_finance.fct_kpn_penalty_invoice\
Key columns  : invoice_number, period, product_code\
\
What to test\
Confirm that DELETE FROM if_finance.fct_kpn_penalty_invoice and the subsequent INSERT are wrapped in a single transaction or equivalent safeguard, since the DELETE has no WHERE clause and removes all history before the INSERT runs.\
\
Pass criteria\
If the INSERT step fails or returns zero rows, the table must retain its prior data (delete and insert succeed or fail together).\
\
Failure means\
A failed or partial load after the DELETE leaves the finance-facing table empty or incomplete with no automatic rollback, silently breaking downstream reporting.\
