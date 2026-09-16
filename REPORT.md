# Design Report

## 1. Architecture

Single Python process, two modes (`discover`, `replay`), no services/queues —
justified by the brief's own steer ("we don't reward... building scaling
infrastructure"; "simpler is fine if justified"). A real deployment would
run replay behind an API (see stretch-goal note in §7), but that's additive
packaging around the same engine, not a different engine.

Layering, innermost to outermost:

- **`agent/perception.py`** — turns a live Playwright `page` into a snapshot
  of *interactive elements* (role + accessible name + a per-turn ref id) plus
  a plain-text summary. This is the one piece that would change per surface
  type (see §4) — everything above it only ever sees refs/roles/names, never
  raw markup.
- **`agent/actions.py`** — the one actuator. Given a `Locator` (an ordered
  fallback chain of strategies), resolves and executes. Both discovery and
  replay call through here, so "how we act on a surface" has exactly one
  implementation, and a locator recorded during discovery is *the same kind
  of object* replay resolves later — no translation step to drift out of
  sync.
- **`agent/guardrails.py`** — allowlist (domain/route/action-type) and risk
  classification (safe-reversible vs. risky-irreversible, by matching the
  *target control's* accessible name against configured patterns). Both
  discovery and replay call this before every action; it is not something
  either the model or the artifact can opt out of.
- **`agent/llm_agent.py`** — the observe→decide→act loop. Provider-agnostic
  via `agent/llm_providers.py` (Anthropic Messages API or any OpenAI-compatible
  Chat Completions endpoint — used here for Gemini's free tier; see below).
  Produces a `Capability`.
- **`agent/replay.py`** — walks a `Capability`'s steps with no model calls,
  substituting parameters, resolving locators, checking checkpoints,
  classifying outcomes. Returns a `ReplayResult`.
- **`agent/escalation.py`** — shared by both: raises an `InterventionRequest`
  against a live `page`, hands it to an operator console, resumes.

**Trade-off — provider swap mid-project.** I originally built this against
Anthropic's Messages API (tool-use), which is what the loop's structure is
most naturally shaped around. When the available Anthropic account had no
API credit, I added `agent/llm_providers.py` as a thin adapter so the same
loop runs against any OpenAI-compatible Chat Completions endpoint, and used
Google's Gemini free tier (via its OpenAI-compat endpoint) for the actual
evidence in `/evidence/`. This cost an afternoon but is a real example of
the "surface/seam" discipline in §4 paying off in miniature: because the
loop only depends on an abstract `observe/report_result` interface, not on
Anthropic's SDK types, swapping providers touched one new file and a handful
of lines in `llm_agent.py`, not the discovery logic itself. `LLM_PROVIDER`
and `LLM_MODEL` are env-configured; both providers are supported going
forward, not just the one used for this submission's evidence.

**Trade-off — discovery declares the contract, not the model.** The
developer/operator kicking off discovery supplies the goal, concrete
parameter values for *this* trial run, and the output names/descriptions the
capability must produce (`scenarios.py`). Discovery's job is to find a UI
path that satisfies that pre-declared contract, not to invent one. This
keeps the resulting `Capability.parameters`/`outputs` well-typed and
intentional rather than whatever the model happened to name things, at the
cost of needing a human to declare the contract before the first run.

## 2. Artifact schema

`agent/schemas.py::Capability` is the reusable, agent-invocable unit. Design
choices:

- **Locator is a ranked fallback chain, not a single selector.** Each entry
  is `{strategy, value[, role]}` — `role` (accessibility role + name) first,
  then a raw `name=` attribute if the legacy markup happens to have one,
  then `nth-of-tag` as a last resort. Replay tries each in order and *logs
  which one matched* — so drift shows up as "fell back to strategy 2" in the
  evidence trail well before it becomes an outright failure, rather than
  either silently succeeding forever or failing all at once with no signal.
- **Steps carry `risk_level` and `requires_human_approval` per step**, not
  just a whole-capability flag — one flow can be mostly safe/reversible with
  exactly one irreversible tail step gated on a human (see §5). This is
  central, not incidental: it's what lets "reach the confirmation screen"
  and "finalize the account" be the *same* underlying flow with one
  additional, explicitly gated step, rather than two unrelated recordings.
- **Parameters and outputs are typed and separately flagged `sensitive`.**
  A sensitive parameter's example value is stripped before the artifact is
  ever written to disk (`DiscoveryAgent._build_capability`), and a sensitive
  output is redacted in the replay result log. The artifact schema is the
  enforcement point for "never persist secrets," not a policy someone has to
  remember to apply at the call site.
- **`business_outcomes` is part of the artifact, not the replay engine.**
  Each capability declares its own known-legitimate non-success patterns
  (`match_text_contains` against the live page text). This keeps the
  business-outcome taxonomy *per flow*, matching reality (what "no such
  record" looks like is a property of this specific app screen, not
  something replay can know generically), while the *mechanism* for
  checking them is shared.
- **`approval_state: draft|approved` + `Provenance{discovered_at,
  discovered_by_model, source_run_id, reviewed, reviewer}`.** A capability
  always know who/what produced it and whether a human has signed off — see
  §7 for why this matters given a real mistake in the discovery evidence.
- **Versioned, not mutated.** `bump_version()` deep-copies; the raw
  discovery output and every reviewed revision are separate files
  (`lookup_member_balance.v1.json`, `.v2.json`) — reviewable independently,
  and it's possible to see exactly what a review changed by diffing them.

## 3. Determinism & error handling

Determinism comes from three things together: (1) no model call anywhere in
`replay.py`; (2) the same locator-fallback-chain actuator discovery used to
act, so replay isn't reinterpreting the recording through a different
execution path; (3) explicit `timeout_ms` per step and a `Checkpoint`
(text/URL assertion) after any step that declares one, so replay never
"assumes the click worked."

The result contract (`ReplayResult.status`) is the taxonomy the whole system
is organized around:

- **`business_outcome`** — checked *first*, before letting anything look
  like a failure: after every state-changing step, the current page text is
  matched against the capability's own declared patterns
  (`_detect_business_outcome`). A "no such member" page short-circuits
  immediately, rather than falling through to whatever the next step's
  locator would have done against unexpected content.
- **`hard_failure`** — anything else: a locator whose full fallback chain
  didn't resolve, or a checkpoint assertion that didn't match and isn't a
  declared business outcome. Carries `step_id`, `expected` vs. `observed`,
  and a screenshot (`FailureDetail`) — enough to debug without re-running.
- **recoverable** — currently one case, session/timeout expiry: if a step's
  locator fails to resolve *and* the page has redirected to the login URL,
  replay treats that as recoverable, calls an injected `SessionProvider` to
  re-authenticate (credentials from env, never logged or persisted — see
  §6), and retries the step once. This is intentionally narrow rather than a
  generic retry-everything policy, because most failures in this environment
  are *not* transient (see the glossary's point about legacy UIs being
  stable) — retrying a genuinely wrong locator would just waste time before
  reporting the same hard failure.

**A real bug this surfaced, worth stating plainly.** My first version
checked for session-expiry unconditionally after every successful step
(`is_login_url(page.url)`), which false-positived on the login flow's own
steps — filling the username/password fields *is* legitimately being on the
login page. The first live replay hit this immediately: it "recovered" from
a session that hadn't expired, re-logged in mid-flow, and ended up filling
the wrong field on the wrong page. Fixed by moving the check into the
`ElementNotFound` exception path only — recoverable detection now fires
exactly when a step's own target genuinely isn't on the page and that page
turns out to be the login screen, not on every step that happens to execute
while on it. A regression test (`test_drifted_extract_locator_is_hard_failure_not_a_crash`
and the session-expiry fix) now covers both the original bug's symptom and
the fix. `evidence/replay_878892ff/` shows a deliberately drifted locator
producing a clean `hard_failure` instead of a crash — the same code path
that a second bug (an unguarded `extract` call outside the try/except) used
to crash on before I caught and fixed it during evidence generation.

**Secondary: UI drift.** Not the focus per the brief (these are stable
enterprise apps), but the same fallback chain that improves robustness
within one run also gives a drift signal across runs: if a capability starts
consistently resolving via its 2nd or 3rd locator strategy instead of its
1st, that's a review trigger, loggable from `run.log.jsonl` without any
extra instrumentation.

## 4. Heterogeneity & multi-tenant

**Surface abstraction.** The seam is exactly the perception/actuator
boundary in §1: `perception.snapshot()` returns `(elements, url, title,
text)` and `actions.py` executes against a `Locator`. Nothing above that
line — the discovery loop, the `Capability` schema, the replay engine —
knows Playwright exists. For a legacy web app with worse markup (framesets,
nested tables, no semantic tags), the same `perception.py` still works
because it derives role/name from browser-computed accessibility semantics,
not from CSS classes or test IDs — that's exactly what this submission's mock
app already exercises (no test IDs anywhere, `<input>`/`<button>` roles
inferred from tag, `name=` attributes used only as a fallback). For a
**desktop app**, the same shape applies with a different backend: swap
`perception.py`'s Playwright DOM query for an OS accessibility API query
(UIA on Windows, AX on macOS) that yields the same `(role, name, ref)`
shape, and swap `actions.py`'s Playwright calls for the OS automation
equivalent. `Locator.strategies` already has `role` as its primary strategy
specifically because that concept transfers to desktop UIs, not just
browsers — a screenshot+coordinates fallback would slot in the same way, as
one more strategy entry.

**Multi-tenant reuse.** `TargetApp{app_id, vendor_product, tenant_id}` is
deliberately three fields, not one URL: `vendor_product` identifies the
underlying product (e.g. "CoreBankAdminConsole") independent of which
institution runs it; `tenant_id: null` marks a *base* recording, not tied to
one tenant. The intended reuse model (not built, per the brief's explicit
"we don't expect you to implement multi-tenant" scope note, but the schema
doesn't block it): a base capability keyed by `vendor_product` is looked up
first; a tenant-specific override (same `vendor_product`, `tenant_id` set)
is checked before it and, if present, wins — same idea as CSS specificity or
config layering. An override only needs to carry the steps that actually
differ (e.g. a different `base_url`, a rebranded button label changing which
locator strategy matches first), not a full re-recording. **Drift
detection** falls out of the locator fallback logging already in place: if a
tenant's replay of a base capability starts consistently falling back to
strategy 2/3 instead of matching on strategy 1, or starts hitting
`hard_failure` at a step that other tenants pass, that's the per-tenant-drift
signal — surfaced identically to plain UI drift within one tenant, which is
the right outcome, since from the system's point of view they're the same
phenomenon (recorded expectations diverging from live app state) with
different scopes.

## 5. Escalation & handoff

**Detecting stuck.** Two independent triggers, both real in this
submission, not simulated: (1) *during discovery*, the model itself calls
`finish(status="stuck", reason=...)` when the system prompt's guidance ("do
not retry a blocked action; if there's truly no safe path, stop") applies —
guardrails blocking a risky control is the expected way this fires, not an
error path bolted on after the fact. (2) *during replay*, any step flagged
`requires_human_approval` unconditionally raises an intervention before
attempting the action — this isn't a failure-triggered escalation, it's a
policy gate that runs whether or not the action would have succeeded.

**Taking control of the live session.** `EscalationSession` wraps the same
Playwright `page` object replay/discovery was already driving — the human
operator's `run_operator_console` executes through the identical
`agent/actions.py` functions, against that same `page`, not a fresh browser
or a reconstructed session. `control_owner` (`agent`/`replay`/`human`) marks
who's driving; `InterventionRequest` carries goal, capability id, step id,
the failing/blocking reason, current URL/title, and a screenshot, persisted
to `evidence/<run>/intervention_<id>.json` the moment control changes hands
— so context survives even if the process were to crash mid-handoff. Every
human action (`click`, `fill`, `select`, free-text `note`) is logged into
the *same* `run.log.jsonl` the automation was already writing to, with
`actor="human"` — one continuous timeline, not two systems to reconcile
after the fact (see `evidence/replay_3e39ebcd/run.log.jsonl` for the full
sequence: automation runs steps 0–9 and 11, pauses, hands off, the operator
clicks the confirm button, control resumes, replay verifies the checkpoint
and extracts the final output).

**Handing control back.** `resume(owner="replay")` flips `control_owner`
back and replay re-checks the step's `checkpoint` against the *current* page
state rather than assuming the human's action succeeded — same discipline
as every other step. If the checkpoint fails (or the operator sends
`abort`), that's reported as a normal `hard_failure`/aborted result with the
intervention id attached, not a silent continue.

**What's mocked, deliberately.** The operator "console" is a small command
protocol (`click <ref>`, `fill <ref> <text>`, `note`, `resume`, `abort`),
runnable interactively from a terminal (the real path) or fed a scripted
command list (`--escalation-script`, used only to make this submission's
evidence reproducible without a person present during evidence generation —
same code, same log format, different input source). A real product would
replace the input source with a websocket-driven operator UI and possibly
real-time screen streaming; that's explicitly out of scope per the brief,
and the seam (`run_operator_console`'s `input_fn`/`commands` parameter) is
where that would plug in without touching the control-transfer model itself.

## 6. Safety

**Allowlist** (`AllowlistConfig`): domain, route-prefix, and action-type
allowlists, checked before every action in both discovery and replay
(`check_allowlist`, called from `_execute_tool` and `_execute_step`
respectively — not just one of them). A violation raises and is treated as
a hard failure/blocked action, never silently skipped.

**Risk classification** (`classify_control_risk`): matches the *target
control's accessible name* against configured risky-pattern substrings
("confirm & open account", "delete", "approve transfer", ...) —
deliberately keyed off what the control says it does, not off the route,
since in this environment (and in real legacy admin consoles) the same
route can host both a review screen and its own submit action. This
classification applies to **every action type**, not just `click` — during
the real discovery run, the model tried to `extract` (read-only!) the
confirm button's own label, and guardrails blocked that too. That's a
deliberately conservative choice: not because reading a label is dangerous,
but because letting a recorded artifact reference a risky control at all —
even just to read it — creates a latent path to acting on it in some other
context. The cost is real (it forced a wrong fallback extraction that a
human then had to catch and fix, see §7); I judged that cost acceptable
given what it's defending against.

**Risky ⇒ never auto-executed.** `Step.requires_human_approval` is not a
suggestion the replay engine can route around — it's checked before any
other branch in the replay loop, and there is no configuration path that
lets replay execute such a step unattended. Guardrails additionally prevent
*discovery* from ever recording a risky click as a normal step in the first
place (§5). The two mechanisms overlap on purpose: guardrails stop the
model from doing the risky thing during discovery; `requires_human_approval`
stops replay from doing it during production use, even if a human later
hand-adds that step to a reviewed capability (as `build_finalize_capability.py`
does, deliberately, with the flag set).

**Data handling.** `Parameter.sensitive` / `OutputSpec.sensitive` drive two
independent redaction points: (1) a sensitive parameter's example value is
stripped before an artifact is ever serialized to disk — a saved capability
never contains a credential, even in a draft; (2) `RunLogger`/`redact_mapping`
key-pattern-matches (`password|secret|token|ssn|...`) as a backstop over
*every* log line regardless of whether the schema-level flag was set
correctly, so a mistake in one place doesn't leak through the other.
Credentials for actually running things (mock app operator login) come from
environment variables read at call time, never appear as a literal in code
or in a prompt template, and the one place they're typed into the live page
(`MockBankSessionProvider.login`) is never itself logged.

**Limits.** The allowlist is process-local config, not enforced by a proxy
in front of the browser — a determined bypass at the network layer isn't
prevented, only discouraged at the automation-decision layer. Risk
classification is pattern-based on control text; a mislabeled irreversible
button ("Proceed" instead of "Confirm & Open Account") would not be caught
today — a production version would want this configured per-app by a human
reviewer, not left to a generic pattern list. Redaction is key/field-name
based, not content-based (e.g. it wouldn't catch a raw SSN typed into an
unrelated field) — real deployment would want field-level sensitivity
declared at the app-integration layer, not inferred from names.

## 7. Cuts

- **Multi-tenant and desktop support are design-only, not built** — matches
  the brief's explicit scope note. §4 is the credible-story requirement;
  building either would have meant less depth on the artifact
  schema/replay/escalation trio the brief calls load-bearing.
- **The operator console is a command protocol, not a UI** — real but
  minimal, per the brief's explicit allowance. See §5 for what's real about
  it (same session, same log, same action functions) vs. what's stubbed
  (no visual co-browsing).
- **One retry, one recoverable condition class (session timeout).** I did
  not build a generic retry/backoff policy for arbitrary transient
  failures, because in this environment (stable enterprise UIs) most
  failures are not transient, and a generic retry would mostly just delay
  reporting a real hard failure. If I extended this, "transient slow load"
  (wait-and-retry once on a timeout specifically, distinct from
  element-not-found) would be the next recoverable class to add.
- **No stretch goals attempted beyond what the core naturally produced.**
  `approval_state`/`Provenance.reviewed` (confidence & approval) and the
  finalize-capability's human-gated step came out of doing the core
  requirements honestly (see below), rather than being separately built.
  Given more time, "assisted fallback" (a bounded, single-step LLM recovery
  on a replay hard-failure, policy-checked and logged as evidence) is the
  one I'd build next — it's the natural complement to the taxonomy already
  in `replay.py`.
- **The discovery evidence includes a real mistake, kept rather than
  hidden.** The raw `lookup_member_balance.v1.json` extracted the wrong
  element (a stale ref pointed at a link, not the balance text) and the
  model self-reported success anyway with a plausible-sounding number in
  its reasoning that didn't actually come from the extraction.
  `open_sub_account_reach_confirmation.v1.json` has a related but distinct
  issue: guardrails correctly blocked reading the risky confirm button's
  label, and the model's fallback extraction grabbed the wrong (but
  harmless) element. Both are fixed via a small, explicit, re-runnable
  review script (`scripts/review_*.py`) that bumps the version and flips
  `approval_state` to `approved` — I judged that showing the real failure
  mode and the real review step that catches it is more honest, and more
  useful evidence for this evaluation, than quietly re-running until a
  clean take appeared. It's also the strongest argument in this report for
  why `approval_state` and independent-of-the-model checkpoint verification
  exist at all: the model's own "success" self-report was wrong twice, and
  the schema/process caught it both times.
