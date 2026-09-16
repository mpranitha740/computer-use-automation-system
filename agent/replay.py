"""
Deterministic replay: execute a saved Capability with no LLM in the loop.

Error taxonomy (this is the load-bearing design decision -- see REPORT.md
section 3):

  1. business_outcome  - the app told us something legitimate and expected
     (e.g. "no such member", "access denied"). Detected by matching the
     capability's own declared `business_outcomes` patterns against the
     current page text. Checked *before* any locator/checkpoint failure is
     allowed to look like a crash, so "not found" never gets reported as a
     hard failure.
  2. recoverable        - a transient/known condition the engine can resolve
     itself and continue (currently: session-timeout redirect to /login,
     resolved via an injected SessionProvider that owns credentials -- the
     engine never sees or logs the raw password).
  3. hard_failure        - anything else: element not found after the full
     locator fallback chain, or a checkpoint assertion that didn't match and
     doesn't correspond to a declared business outcome. Carries step id,
     expected vs. observed, and a screenshot for debugging.

A step flagged `requires_human_approval` is never auto-executed: replay
raises an intervention and blocks on `EscalationSession.run_operator_console`
before continuing, using the *same* page/session the automation had open.
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any, Optional, Protocol

from playwright.sync_api import sync_playwright

from agent import actions, perception
from agent.escalation import EscalationSession
from agent.guardrails import AllowlistConfig, GuardrailViolation, check_allowlist
from agent.logging_utils import RunLogger
from agent.schemas import (
    ActionType, Capability, FailureDetail, ReplayResult, ReplayStatus,
)


class SessionProvider(Protocol):
    def login(self, page) -> None: ...
    def is_login_url(self, url: str) -> bool: ...


class ParamValidationError(Exception):
    pass


def validate_params(capability: Capability, params: dict[str, Any]) -> None:
    for p in capability.parameters:
        if p.required and p.name not in params:
            raise ParamValidationError(f"missing required parameter {p.name!r}")
    for name, value in params.items():
        spec = next((p for p in capability.parameters if p.name == name), None)
        if spec is None:
            raise ParamValidationError(f"unexpected parameter {name!r}")
        if spec.type.value == "number" and not isinstance(value, (int, float)):
            raise ParamValidationError(f"parameter {name!r} must be a number")
        if spec.type.value == "boolean" and not isinstance(value, bool):
            raise ParamValidationError(f"parameter {name!r} must be a boolean")
        if spec.type.value == "string" and not isinstance(value, str):
            raise ParamValidationError(f"parameter {name!r} must be a string")


def _substitute(template: Optional[str], params: dict[str, Any]) -> Optional[str]:
    if template is None:
        return None
    out = template
    for name, value in params.items():
        out = out.replace(f"{{{{{name}}}}}", str(value))
    return out


def _detect_business_outcome(capability: Capability, text_summary: str):
    for pattern in capability.business_outcomes:
        if pattern.match_text_contains in text_summary:
            return pattern
    return None


def _check_checkpoint(page, checkpoint) -> tuple[bool, str, str]:
    """Returns (passed, expected_desc, observed_desc)."""
    if checkpoint is None:
        return True, "", ""
    if checkpoint.expect_url_contains:
        ok = checkpoint.expect_url_contains in page.url
        if not ok:
            return False, f"url contains {checkpoint.expect_url_contains!r}", page.url
    if checkpoint.expect_text_contains:
        text = page.eval_on_selector("body", "(el) => el.innerText") or ""
        ok = checkpoint.expect_text_contains in text
        if not ok:
            return False, f"page text contains {checkpoint.expect_text_contains!r}", text[:300]
    return True, "", ""


def replay(
    capability: Capability,
    params: dict[str, Any],
    *,
    evidence_dir: Path,
    allowlist: AllowlistConfig,
    session_provider: Optional[SessionProvider] = None,
    escalation_commands: Optional[list[dict]] = None,
    headless: bool = True,
) -> ReplayResult:
    validate_params(capability, params)
    run_id = f"replay_{uuid.uuid4().hex[:8]}"
    logger = RunLogger(evidence_dir, run_id, kind="replay")
    safe_params = {k: (v if not _is_sensitive(capability, k) else "***") for k, v in params.items()}
    logger.log("system", "run_start", capability_id=capability.capability_id,
               capability_name=capability.name, version=capability.version, params=safe_params)

    t0 = time.time()
    outputs: dict[str, Any] = {}
    result: Optional[ReplayResult] = None
    session_refresh_attempted = False

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context()
        page = context.new_page()
        escalation = EscalationSession(page, logger, run_id)

        try:
            for step in capability.steps:
                if step.requires_human_approval:
                    logger.log("replay", "step_requires_human", step_id=step.id, description=step.description)
                    req = escalation.raise_intervention(
                        reason=f"Step {step.id} ({step.description}) requires human approval before "
                               f"an irreversible action is taken.",
                        capability_id=capability.capability_id, goal=capability.description, step_id=step.id,
                    )
                    escalation.run_operator_console(req, commands=escalation_commands)
                    escalation.resume(owner="replay")
                    if req.status == "aborted":
                        result = ReplayResult(
                            status=ReplayStatus.HARD_FAILURE, capability_id=capability.capability_id,
                            capability_version=capability.version, run_id=run_id,
                            failure=FailureDetail(step_id=step.id, step_description=step.description,
                                                   expected="human approval", observed="operator aborted"),
                            intervention_request_id=req.id,
                        )
                        break
                    logger.log("replay", "resumed_after_human", step_id=step.id)
                    passed, expected, observed = _check_checkpoint(page, step.checkpoint)
                    logger.log("replay", "checkpoint", step_id=step.id, passed=passed)
                    if not passed:
                        result = _hard_failure(
                            capability, run_id, step,
                            FailureDetail(step_id=step.id, step_description=step.description,
                                          expected=expected, observed=observed),
                            page, logger,
                        )
                        break
                    if step.output_name:
                        extract_outcome = _safe_extract(page, step, outputs, logger)
                        if extract_outcome is not None:
                            _, payload = extract_outcome
                            result = _hard_failure(capability, run_id, step, payload, page, logger)
                            break
                    continue

                outcome = _execute_step(
                    page, step, params, capability, allowlist, logger, session_provider,
                )
                if outcome is not None:
                    kind, payload = outcome
                    if kind == "business_outcome":
                        result = ReplayResult(
                            status=ReplayStatus.BUSINESS_OUTCOME, capability_id=capability.capability_id,
                            capability_version=capability.version, run_id=run_id,
                            business_outcome_code=payload.code, business_outcome_message=payload.description,
                        )
                        break
                    if kind == "session_expired" and session_provider is not None and not session_refresh_attempted:
                        session_refresh_attempted = True
                        logger.log("replay", "session_refresh_attempt", step_id=step.id)
                        session_provider.login(page)
                        outcome2 = _execute_step(page, step, params, capability, allowlist, logger, session_provider)
                        if outcome2 is not None:
                            kind2, payload2 = outcome2
                            if kind2 == "business_outcome":
                                result = ReplayResult(status=ReplayStatus.BUSINESS_OUTCOME,
                                                       capability_id=capability.capability_id,
                                                       capability_version=capability.version, run_id=run_id,
                                                       business_outcome_code=payload2.code,
                                                       business_outcome_message=payload2.description)
                            else:
                                result = _hard_failure(capability, run_id, step, payload2, page, logger)
                            break
                        continue
                    result = _hard_failure(capability, run_id, step, payload, page, logger)
                    break
                if step.output_name:
                    extract_outcome = _safe_extract(page, step, outputs, logger)
                    if extract_outcome is not None:
                        _, payload = extract_outcome
                        result = _hard_failure(capability, run_id, step, payload, page, logger)
                        break

            if result is None:
                passed, expected, observed = _check_checkpoint(page, capability.success_checkpoint)
                if passed:
                    result = ReplayResult(
                        status=ReplayStatus.SUCCESS, capability_id=capability.capability_id,
                        capability_version=capability.version, run_id=run_id, outputs=outputs,
                    )
                else:
                    bo = _detect_business_outcome(capability, page.eval_on_selector("body", "(el) => el.innerText") or "")
                    if bo:
                        result = ReplayResult(status=ReplayStatus.BUSINESS_OUTCOME, capability_id=capability.capability_id,
                                               capability_version=capability.version, run_id=run_id,
                                               business_outcome_code=bo.code, business_outcome_message=bo.description)
                    else:
                        shot = logger.screenshot_path("success_checkpoint_failed")
                        page.screenshot(path=str(shot))
                        result = ReplayResult(
                            status=ReplayStatus.HARD_FAILURE, capability_id=capability.capability_id,
                            capability_version=capability.version, run_id=run_id,
                            failure=FailureDetail(step_id="success_checkpoint", step_description="final success checkpoint",
                                                   expected=expected, observed=observed, screenshot_path=str(shot)),
                        )
        finally:
            browser.close()

    result.duration_ms = int((time.time() - t0) * 1000)
    logger.log("system", "run_end", status=result.status.value, outputs={k: "[redacted]" if _is_sensitive(capability, k) else v
                                                                            for k, v in result.outputs.items()},
               business_outcome_code=result.business_outcome_code)
    logger.save_json("result.json", result.model_dump())
    logger.close()
    return result


def _is_sensitive(capability: Capability, output_name: str) -> bool:
    spec = next((o for o in capability.outputs if o.name == output_name), None)
    return bool(spec and spec.sensitive)


def _execute_step(page, step, params, capability, allowlist, logger, session_provider):
    """Returns None on success, or (kind, payload) where kind is
    'business_outcome' | 'session_expired' | 'hard_failure'."""
    try:
        if step.action == ActionType.NAVIGATE:
            url = _substitute(step.value_template, params)
            check_allowlist(allowlist, url=url, action_type=ActionType.NAVIGATE)
            actions.do_navigate(page, url)
        elif step.action == ActionType.CLICK:
            actions.do_click(page, step.locator, timeout_ms=step.timeout_ms)
        elif step.action == ActionType.FILL:
            value = _substitute(step.value_template, params)
            actions.do_fill(page, step.locator, value, timeout_ms=step.timeout_ms)
        elif step.action == ActionType.SELECT_OPTION:
            value = _substitute(step.value_template, params)
            actions.do_select_option(page, step.locator, value, timeout_ms=step.timeout_ms)
        elif step.action == ActionType.EXTRACT:
            pass  # handled by caller after outcome check, to allow business-outcome short circuit first
        elif step.action == ActionType.ASSERT_TEXT:
            pass

        logger.log("replay", "step_ok", step_id=step.id, action=step.action.value)

        text = page.eval_on_selector("body", "(el) => el.innerText") or ""
        bo = _detect_business_outcome(capability, text)
        if bo:
            logger.log("replay", "business_outcome_detected", step_id=step.id, code=bo.code)
            return "business_outcome", bo

        if step.checkpoint:
            passed, expected, observed = _check_checkpoint(page, step.checkpoint)
            logger.log("replay", "checkpoint", step_id=step.id, passed=passed, expected=expected)
            if not passed:
                return "hard_failure", FailureDetail(step_id=step.id, step_description=step.description,
                                                       expected=expected, observed=observed)
        return None
    except GuardrailViolation as exc:
        logger.log("system", "guardrail_blocked", step_id=step.id, error=str(exc))
        return "hard_failure", FailureDetail(step_id=step.id, step_description=step.description,
                                              expected="action within allowlist", observed=str(exc))
    except actions.ElementNotFound as exc:
        if session_provider is not None and session_provider.is_login_url(page.url):
            # The control we expected simply isn't on the page because the session
            # dropped us back at the login screen mid-flow -- not a drifted locator.
            logger.log("replay", "session_expired_detected", step_id=step.id, url=page.url)
            return "session_expired", None
        logger.log("replay", "element_not_found", step_id=step.id, tried=exc.tried)
        return "hard_failure", FailureDetail(step_id=step.id, step_description=step.description,
                                              expected=f"locator to resolve: {exc.locator.description}",
                                              observed=f"no strategy matched; tried {exc.tried}")
    except Exception as exc:  # noqa: BLE001
        logger.log("replay", "step_error", step_id=step.id, error=str(exc))
        return "hard_failure", FailureDetail(step_id=step.id, step_description=step.description,
                                              expected="step to execute without error", observed=str(exc))


def _safe_extract(page, step, outputs: dict, logger):
    """Runs an EXTRACT step's actual read, isolated from the outcome-detection
    pass in _execute_step so a drifted/missing locator here also comes back as
    a clean hard_failure instead of an unhandled exception."""
    try:
        text, _ = actions.do_extract(page, step.locator, timeout_ms=step.timeout_ms)
        outputs[step.output_name] = text
        logger.log("replay", "extract_ok", step_id=step.id, output_name=step.output_name)
        return None
    except actions.ElementNotFound as exc:
        logger.log("replay", "element_not_found", step_id=step.id, tried=exc.tried)
        return "hard_failure", FailureDetail(step_id=step.id, step_description=step.description,
                                              expected=f"locator to resolve: {exc.locator.description}",
                                              observed=f"no strategy matched; tried {exc.tried}")
    except Exception as exc:  # noqa: BLE001
        logger.log("replay", "step_error", step_id=step.id, error=str(exc))
        return "hard_failure", FailureDetail(step_id=step.id, step_description=step.description,
                                              expected="output to extract without error", observed=str(exc))


def _hard_failure(capability, run_id, step, payload: FailureDetail, page, logger) -> ReplayResult:
    shot = logger.screenshot_path(f"failure_{step.id}")
    try:
        page.screenshot(path=str(shot))
        payload.screenshot_path = str(shot)
    except Exception:
        pass
    return ReplayResult(
        status=ReplayStatus.HARD_FAILURE, capability_id=capability.capability_id,
        capability_version=capability.version, run_id=run_id, failure=payload,
    )
