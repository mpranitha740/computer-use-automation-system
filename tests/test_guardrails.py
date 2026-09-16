import pytest

from agent.guardrails import (
    AllowlistConfig, GuardrailViolation, check_allowlist, classify_control_risk,
    redact, redact_mapping,
)
from agent.schemas import ActionType, RiskLevel


def test_allowed_domain_and_action_passes():
    cfg = AllowlistConfig()
    check_allowlist(cfg, url="http://127.0.0.1:5055/members/search", action_type=ActionType.CLICK)


def test_disallowed_domain_blocked():
    cfg = AllowlistConfig()
    with pytest.raises(GuardrailViolation):
        check_allowlist(cfg, url="http://evil.example.com/", action_type=ActionType.CLICK)


def test_disallowed_action_type_blocked():
    cfg = AllowlistConfig(allowed_action_types=[ActionType.NAVIGATE])
    with pytest.raises(GuardrailViolation):
        check_allowlist(cfg, url="http://127.0.0.1:5055/", action_type=ActionType.CLICK)


def test_disallowed_route_prefix_blocked():
    cfg = AllowlistConfig(allowed_route_prefixes=["/members"])
    with pytest.raises(GuardrailViolation):
        check_allowlist(cfg, url="http://127.0.0.1:5055/admin/danger", action_type=ActionType.CLICK)


@pytest.mark.parametrize("name", [
    "Confirm & Open Account", "confirm & open account", "  Delete  ", "Approve Transfer",
])
def test_risky_controls_classified_irreversible(name):
    cfg = AllowlistConfig()
    assert classify_control_risk(cfg, accessible_name=name) == RiskLevel.RISKY_IRREVERSIBLE


@pytest.mark.parametrize("name", ["Search", "Continue", "Back to search", "Open new sub-account"])
def test_normal_controls_classified_safe(name):
    cfg = AllowlistConfig()
    assert classify_control_risk(cfg, accessible_name=name) == RiskLevel.SAFE_REVERSIBLE


def test_redact_masks_middle_of_value():
    assert redact("demo-pass", sensitive=True) == "d*******s"
    assert redact("demo-pass", sensitive=False) == "demo-pass"


def test_redact_mapping_masks_password_key_even_if_not_flagged():
    out = redact_mapping({"password": "demo-pass", "member_id": "12345"})
    assert out["password"] != "demo-pass"
    assert out["member_id"] == "12345"


def test_redact_mapping_recurses_into_nested_dicts():
    out = redact_mapping({"detail": {"token": "abc123xyz"}})
    assert out["detail"]["token"] != "abc123xyz"
