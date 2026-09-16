"""
Human/reviewer step: take the discovered "reach confirmation screen" capability
and produce a second, explicitly-reviewed capability that finalizes the
sub-account -- by appending one step that clicks the irreversible "Confirm &
Open Account" button, flagged `requires_human_approval=True`.

This is deliberately NOT something discovery does itself: guardrails block the
agent from ever clicking a risky/irreversible control (see
agent/guardrails.py, classify_control_risk), so a discovery run can never
produce this step on its own. Authoring it is a reviewer action -- the same
way a human would review a recorded flow and explicitly decide "this last
step is fine to keep, but only with a human present" before approving the
capability for use. That's the intended workflow: discovery finds the path,
a human decides which parts of that path may run unattended.

Run after `python cli.py discover open_sub_account_reach_confirmation`.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.schemas import ActionType, Capability, Checkpoint, Locator, OutputSpec, ParamType, RiskLevel, Step

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "artifacts" / "open_sub_account_reach_confirmation.v2.json"
DST = ROOT / "artifacts" / "open_sub_account_and_finalize.v1.json"


def main():
    if not SRC.exists():
        print(f"Run `python cli.py discover open_sub_account_reach_confirmation` and "
              f"`python scripts/review_open_sub_account.py` first to produce {SRC}", file=sys.stderr)
        sys.exit(1)
    base = Capability.model_validate_json(SRC.read_text(encoding="utf-8"))

    finalize_step = Step(
        id="step_finalize",
        description="Click 'Confirm & Open Account' to finalize the new sub-account",
        action=ActionType.CLICK,
        locator=Locator(
            strategies=[{"strategy": "role", "role": "button", "value": "Confirm & Open Account"}],
            description="the final confirm/submit button on the confirmation screen",
        ),
        risk_level=RiskLevel.RISKY_IRREVERSIBLE,
        requires_human_approval=True,
        checkpoint=Checkpoint(
            description="New sub-account was actually opened",
            expect_text_contains="Sub-account opened successfully",
        ),
    )
    extract_step = Step(
        id="step_extract_account_number",
        description="Extract the newly opened account number",
        action=ActionType.EXTRACT,
        locator=Locator(
            strategies=[{"strategy": "css", "value": "p b"}],
            description="bolded account number on the success page",
        ),
        output_name="new_account_number",
    )

    new_cap = base.model_copy(deep=True)
    new_cap.name = "open_sub_account_and_finalize"
    new_cap.version = 1
    new_cap.description = base.description + " Then, WITH HUMAN APPROVAL, finalize the account."
    new_cap.steps = list(base.steps) + [finalize_step, extract_step]
    new_cap.outputs = list(base.outputs) + [
        OutputSpec(name="new_account_number", type=ParamType.STRING,
                   description="Account number of the newly opened sub-account")
    ]
    new_cap.success_checkpoint = Checkpoint(
        description="Sub-account opened successfully page shown",
        expect_text_contains="Sub-account opened successfully",
    )
    new_cap.provenance.reviewed = True
    new_cap.provenance.reviewer = "human-authored-step (see scripts/build_finalize_capability.py)"
    new_cap.approval_state = "approved"

    DST.write_text(json.dumps(new_cap.model_dump(), indent=2, default=str), encoding="utf-8")
    print(f"Wrote {DST}")


if __name__ == "__main__":
    main()
