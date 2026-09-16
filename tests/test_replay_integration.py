"""
Integration tests for the replay engine's error taxonomy, run against the
real mock app (no mocking of Playwright/Flask) so the business-outcome vs.
hard-failure split is verified against actual page content, not fixtures.

This hand-built capability mirrors what `discover lookup_member_balance`
would record, so these tests double as a check that replay's contract holds
even without needing a live LLM call in CI.
"""
from pathlib import Path

import pytest

from agent.guardrails import AllowlistConfig
from agent.replay import ParamValidationError, replay, validate_params
from agent.schemas import (
    ActionType, BusinessOutcomePattern, Capability, Checkpoint, Locator,
    OutputSpec, Parameter, ParamType, Provenance, Step, TargetApp,
)


def make_capability(base_url: str) -> Capability:
    return Capability(
        name="lookup_member_balance",
        description="test capability",
        target=TargetApp(app_id="corebank_admin", vendor_product="CoreBankAdminConsole", base_url=f"{base_url}/login"),
        parameters=[
            Parameter(name="username", type=ParamType.STRING, description="u", example=None, sensitive=True),
            Parameter(name="password", type=ParamType.STRING, description="p", example=None, sensitive=True),
            Parameter(name="member_id", type=ParamType.STRING, description="member id"),
        ],
        outputs=[OutputSpec(name="savings_balance", type=ParamType.STRING, description="balance")],
        steps=[
            Step(id="s1", description="go to login", action=ActionType.NAVIGATE,
                 value_template=f"{base_url}/login"),
            Step(id="s2", description="fill username", action=ActionType.FILL,
                 locator=Locator(strategies=[{"strategy": "css", "value": 'input[name="username"]'}],
                                  description="username field"),
                 value_template="{{username}}"),
            Step(id="s3", description="fill password", action=ActionType.FILL,
                 locator=Locator(strategies=[{"strategy": "css", "value": 'input[name="password"]'}],
                                  description="password field"),
                 value_template="{{password}}"),
            Step(id="s4", description="submit login", action=ActionType.CLICK,
                 locator=Locator(strategies=[{"strategy": "css", "value": 'input[type="submit"]'}],
                                  description="log in button")),
            Step(id="s5", description="go to member detail", action=ActionType.NAVIGATE,
                 value_template=f"{base_url}/members/{{{{member_id}}}}"),
            Step(id="s6", description="extract savings balance", action=ActionType.EXTRACT,
                 locator=Locator(strategies=[{"strategy": "css", "value": "p b"}], description="balance"),
                 output_name="savings_balance"),
        ],
        success_checkpoint=Checkpoint(description="detail page shown", expect_text_contains="Current Savings Balance"),
        business_outcomes=[
            BusinessOutcomePattern(code="member_not_found", description="no such member",
                                    match_text_contains="No member found for ID"),
            BusinessOutcomePattern(code="member_locked", description="locked member",
                                    match_text_contains="Access denied: member"),
        ],
        provenance=Provenance(discovered_at=0.0, discovered_by_model="test", source_run_id="r"),
    )


@pytest.fixture
def capability(mock_app_base_url):
    return make_capability(mock_app_base_url)


@pytest.fixture
def evidence_dir(tmp_path):
    return tmp_path / "evidence"


@pytest.fixture
def allowlist(mock_app_base_url):
    return AllowlistConfig(allowed_domains=["127.0.0.1"])


def _run(capability, params, evidence_dir, allowlist):
    return replay(capability, params, evidence_dir=evidence_dir, allowlist=allowlist, headless=True)


def test_success_path_extracts_balance(capability, evidence_dir, allowlist):
    result = _run(capability, {"username": "operator", "password": "demo-pass", "member_id": "12345"},
                   evidence_dir, allowlist)
    assert result.status.value == "success"
    assert result.outputs["savings_balance"] == "$4,215.30"


def test_member_not_found_is_business_outcome_not_failure(capability, evidence_dir, allowlist):
    result = _run(capability, {"username": "operator", "password": "demo-pass", "member_id": "00000"},
                   evidence_dir, allowlist)
    assert result.status.value == "business_outcome"
    assert result.business_outcome_code == "member_not_found"


def test_locked_member_is_business_outcome_not_failure(capability, evidence_dir, allowlist):
    result = _run(capability, {"username": "operator", "password": "demo-pass", "member_id": "99999"},
                   evidence_dir, allowlist)
    assert result.status.value == "business_outcome"
    assert result.business_outcome_code == "member_locked"


def test_bad_locator_is_hard_failure_with_debug_detail(capability, evidence_dir, allowlist):
    capability.steps[3].locator = Locator(
        strategies=[{"strategy": "css", "value": "input.nonexistent-submit-button"}],
        description="broken locator",
    )
    result = _run(capability, {"username": "operator", "password": "demo-pass", "member_id": "12345"},
                   evidence_dir, allowlist)
    assert result.status.value == "hard_failure"
    assert result.failure.step_id == "s4"
    assert "nonexistent-submit-button" in result.failure.observed


def test_drifted_extract_locator_is_hard_failure_not_a_crash(capability, evidence_dir, allowlist):
    capability.steps[5].locator = Locator(
        strategies=[{"strategy": "css", "value": ".this-class-does-not-exist-anymore"}],
        description="drifted balance locator",
    )
    result = _run(capability, {"username": "operator", "password": "demo-pass", "member_id": "12345"},
                   evidence_dir, allowlist)
    assert result.status.value == "hard_failure"
    assert result.failure.step_id == "s6"


def test_missing_required_param_rejected_before_launching_browser(capability, evidence_dir, allowlist):
    with pytest.raises(ParamValidationError):
        validate_params(capability, {"username": "operator", "password": "demo-pass"})


def test_guardrail_blocks_out_of_allowlist_domain(capability, evidence_dir):
    strict_allowlist = AllowlistConfig(allowed_domains=["only-this-host-is-allowed.invalid"])
    result = _run(capability, {"username": "operator", "password": "demo-pass", "member_id": "12345"},
                   evidence_dir, strict_allowlist)
    assert result.status.value == "hard_failure"
    assert "allowlist" in result.failure.observed
