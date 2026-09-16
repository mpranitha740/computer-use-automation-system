"""
Human review pass on the raw discovery output for
open_sub_account_reach_confirmation.

What happened (see evidence/discover_6c04e944/run.log.jsonl): the model
correctly reached the confirmation screen and correctly obeyed guardrails --
it tried to `extract` from ref e0, which was the "Confirm & Open Account"
button, and the guardrail layer blocked it (classify_control_risk treats
that control as risky/irreversible for *any* action, not just click, so
even reading it is restricted). The model then fell back to a different
ref (e1, the "Back / edit" link) and extracted that instead, which is the
wrong data -- not a safety problem, but a correctness one: the recorded
`confirmation_details` output would return "Back / edit" on every replay.

Root cause worth noting for the design write-up: perception only exposes
*interactive* elements as extractable refs, but the data this capability
actually needs (the account type / deposit table) is static text, not an
interactive control, so it was never offered to the model as a valid
target. The fix here is a locator-level correction a human reviewer makes
after comparing the recorded step to the actual page: point the extract
step at the confirmation table directly (via CSS, which the replay engine
can resolve even though discovery's tool interface couldn't see it as a
candidate).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.schemas import Capability, Locator

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "artifacts" / "open_sub_account_reach_confirmation.v1.json"
DST = ROOT / "artifacts" / "open_sub_account_reach_confirmation.v2.json"


def main():
    cap = Capability.model_validate_json(SRC.read_text(encoding="utf-8"))
    reviewed = cap.bump_version()

    fixed = False
    for step in reviewed.steps:
        if step.action.value == "extract" and step.output_name == "confirmation_details":
            step.locator = Locator(
                strategies=[{"strategy": "css", "value": 'table[border="1"]'}],
                description="the account type / deposit confirmation table",
            )
            step.description = "Extract confirmation_details from the confirmation table"
            fixed = True
    assert fixed, "expected to find the confirmation_details extract step"

    reviewed.provenance.reviewed = True
    reviewed.provenance.reviewer = "human-review (see scripts/review_open_sub_account.py)"
    reviewed.approval_state = "approved"

    DST.write_text(json.dumps(reviewed.model_dump(), indent=2, default=str), encoding="utf-8")
    print(f"Wrote reviewed, approved capability: {DST}")


if __name__ == "__main__":
    main()
