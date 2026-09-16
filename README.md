# Computer-Use Automation System

A small, real, end-to-end implementation of the interface.ai take-home: an LLM
drives a live "legacy" bank admin console to discover a flow, that flow is
recorded as a typed, versioned **capability** artifact, and the artifact is
then **replayed deterministically** (no LLM in the loop) with explicit
business-outcome / recoverable / hard-failure handling and a real
human-escalation & live-session-handoff path.

See [`REPORT.md`](REPORT.md) for the design write-up (architecture, schema,
determinism/error handling, heterogeneity & multi-tenant story, escalation,
safety, and cuts).

## What's in here

```
mock_app/        the target surface: a legacy-styled Flask admin console
                 (server-rendered, table layout, no test IDs)
agent/           schemas, guardrails, perception, actuator, discovery loop,
                 replay engine, escalation/handoff, logging
scenarios.py     the two capability contracts discovery is run against
scripts/         one-off "human reviewer" scripts (see below) + the
                 scripted operator console commands used for the escalation demo
cli.py           `discover` / `replay` / `list` commands
artifacts/       saved capability JSON files (v1 = raw discovery output,
                 v2 = after human review -- both kept, see REPORT.md §3/§7)
evidence/        JSONL logs + screenshots from real discovery and replay runs
tests/           pytest: schema, guardrail, and replay-error-taxonomy tests
                 (run against the real mock app, no LLM needed)
```

## Setup

Requires Python 3.11+.

```bash
python -m venv .venv
. .venv/Scripts/activate        # Windows; use `source .venv/bin/activate` on macOS/Linux
pip install -r requirements.txt
python -m playwright install chromium
```

Copy `.env.example` to `.env` and fill in an API key for **one** provider:

```bash
cp .env.example .env
```

- **Gemini** (what this submission's evidence was produced with): get a free
  key at https://aistudio.google.com/apikey (genuine free tier, no card
  required). Set `LLM_PROVIDER=gemini`, `GEMINI_API_KEY=...`. Gemini's free
  tier is rate/quota-limited per model per day; the discovery agent retries
  on 429 with backoff, but if you exhaust one model's daily quota, swap
  `LLM_MODEL` for another (`gemini-flash-lite-latest`, `gemini-3.7-flash`,
  etc. all work against the same key).
- **Anthropic**: get a key at https://console.anthropic.com (Settings →
  Billing → API keys; needs a small credit balance, separate from any
  Claude.ai subscription). Set `LLM_PROVIDER=anthropic`,
  `ANTHROPIC_API_KEY=...`, `LLM_MODEL=claude-sonnet-5` (or another current
  Claude model).

`.env` is gitignored; nothing in it is ever committed, logged, or written
into a saved artifact (see guardrails / redaction in REPORT.md §6).

## Running without live services

Tests spin up the mock app themselves and never call an LLM — `pytest` is
the "no live services" path:

```bash
pytest -q
```

Everything else (`discover`, `replay`) drives a real (local) live surface by
design — that's the point of the system — so it needs the mock app running.

## Demo path

**1. Start the target app** (leave running in its own terminal):

```bash
python mock_app/app.py
# serves http://127.0.0.1:5055
```

**2. Run a real LLM-driven discovery** (needs your API key from `.env`):

```bash
python cli.py discover lookup_member_balance
```

This logs in, searches for member `12345`, opens their detail page, and
extracts the savings balance -- purely by the model reading the page and
calling tools, no hardcoded steps. It writes:
- `artifacts/lookup_member_balance.v1.json` -- the raw recorded capability
- `evidence/discover_<id>/` -- structured JSONL log + screenshots

**3. Review the artifact.** In the run captured for this submission, the raw
v1 output had one real mistake (a stale-ref extraction bug -- see
`REPORT.md` §3 and §7 for the full story). The review step that fixes it is
itself a real, runnable script:

```bash
python scripts/review_lookup_balance.py
# -> artifacts/lookup_member_balance.v2.json (reviewed, approved)
```

**4. Replay deterministically** -- no LLM involved from here on:

```bash
# success
python cli.py replay artifacts/lookup_member_balance.v2.json member_id=12345

# business outcome: no such member (not a crash)
python cli.py replay artifacts/lookup_member_balance.v2.json member_id=00000

# business outcome: locked/restricted member
python cli.py replay artifacts/lookup_member_balance.v2.json member_id=99999

# hard failure: deliberately drifted locator, to show debuggable failure detail
python cli.py replay artifacts/lookup_member_balance.drift-demo.json member_id=12345
```

**5. The risky/irreversible flow + human escalation.** A second discovery
run drives a multi-step form (open a new sub-account) up to, but not past,
an irreversible confirmation:

```bash
python cli.py discover open_sub_account_reach_confirmation
python scripts/review_open_sub_account.py   # same kind of review fix, see REPORT.md
```

A human reviewer then explicitly extends the reviewed capability with one
more step -- clicking the actual "Confirm & Open Account" button -- flagged
`requires_human_approval: true`. Discovery itself can never produce this
step (guardrails block the agent from ever clicking a risky/irreversible
control), so this is a deliberate, documented human addition:

```bash
python scripts/build_finalize_capability.py
# -> artifacts/open_sub_account_and_finalize.v1.json
```

Replaying it demonstrates the full escalation & handoff: replay pauses
before the irreversible step, hands the *same live browser session* to an
operator console, the operator performs the click, and control returns to
replay to finish and extract the new account number:

```bash
python cli.py replay artifacts/open_sub_account_and_finalize.v1.json \
    member_id=12345 account_type=Savings deposit=75 \
    --escalation-script scripts/operator_finalize_commands.json
```

Drop `--escalation-script` to get the **real, interactive** operator console
instead (reads commands from your terminal: `click <ref>`, `fill <ref>
<text>`, `note <text>`, `resume`, `abort`) -- the script flag exists only to
make the demo reproducible in CI/evidence without a person actually present;
see `agent/escalation.py` and REPORT.md §5 for why both paths run through
the same code.

```bash
# also worth trying:
python cli.py replay artifacts/open_sub_account_reach_confirmation.v2.json \
    member_id=12345 account_type=Savings deposit=-10   # -> validation_error business outcome
```

**6. See it all together:** `evidence/` after the above contains, for every
run, a `run.log.jsonl` (every agent/replay/human action with sensitive
values redacted), a `result.json` (for replays), and screenshots on
failure/escalation. `evidence/replay_3e39ebcd/` is the full escalation
timeline referenced in REPORT.md §5.

## Credentials note

The mock app's operator login (`operator` / `demo-pass`) is a throwaway demo
credential for a local Flask app with no real data behind it -- not a
security-sensitive secret, but handled the same way a real one would be:
sourced from environment variables, never typed into a prompt as a literal
in code, never written into a saved artifact (`Parameter.sensitive=true`
strips the example value before it's persisted), and redacted in every log
line.
