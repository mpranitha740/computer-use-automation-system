import pytest

from agent.schemas import (
    ActionType, Capability, Checkpoint, Locator, OutputSpec, Parameter,
    ParamType, Provenance, Step, TargetApp,
)


def _minimal_capability(**overrides) -> Capability:
    base = dict(
        name="lookup_member_balance",
        description="test",
        target=TargetApp(app_id="a", vendor_product="v", base_url="http://x"),
        parameters=[Parameter(name="member_id", type=ParamType.STRING, description="id", example="1")],
        outputs=[OutputSpec(name="balance", type=ParamType.STRING, description="balance")],
        steps=[Step(id="s1", description="go", action=ActionType.NAVIGATE, value_template="http://x")],
        success_checkpoint=Checkpoint(description="ok", expect_text_contains="Balance"),
        provenance=Provenance(discovered_at=0.0, discovered_by_model="test-model", source_run_id="r1"),
    )
    base.update(overrides)
    return Capability(**base)


def test_capability_round_trips_through_json():
    cap = _minimal_capability()
    dumped = cap.model_dump_json()
    reloaded = Capability.model_validate_json(dumped)
    assert reloaded == cap


def test_capability_version_defaults_to_1_and_bump_increments():
    cap = _minimal_capability()
    assert cap.version == 1
    bumped = cap.bump_version()
    assert bumped.version == 2
    assert cap.version == 1  # original untouched


def test_missing_required_field_rejected():
    with pytest.raises(Exception):
        Capability(name="x")  # missing target/steps/etc.


def test_locator_strategies_are_ordered_fallback_chain():
    loc = Locator(
        strategies=[
            {"strategy": "role", "role": "button", "value": "Search"},
            {"strategy": "css", "value": "input[type=submit]"},
        ],
        description="search button",
    )
    assert loc.strategies[0]["strategy"] == "role"
    assert loc.strategies[-1]["strategy"] == "css"
