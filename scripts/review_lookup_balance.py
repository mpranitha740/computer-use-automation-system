"""
Human review pass on the raw discovery output for lookup_member_balance.

What discovery actually did (see evidence/discover_de1bcce8/run.log.jsonl):
it logged in, searched, but then called `extract` on stale ref "e0" AFTER
clicking through -- at that point e0 no longer pointed at the balance text,
it pointed at the "Open new sub-account" link left over from a prior
snapshot. The model then self-reported success with a plausible-sounding
balance in its `reason` text, without that number actually coming from the
extract call. Replaying the raw v1 artifact would silently return the wrong
`savings_balance` output on every call, even though the visible page (and
the success_checkpoint) looks completely correct.

This is exactly why capabilities have an approval_state and a human review
step before they're trusted for unattended replay (see REPORT.md section 7
/ stretch goal "confidence & approval"): the checkpoint alone isn't enough
to catch a wrong-but-plausible extraction, a human comparing the recorded
step to the actual page is what catches it. This script performs that
review: fix the one bad locator, bump the version, mark it reviewed +
approved, and leave the raw v1 discovery artifact untouched as evidence of
what actually happened.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.schemas import Capability, Locator

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "artifacts" / "lookup_member_balance.v1.json"
DST = ROOT / "artifacts" / "lookup_member_balance.v2.json"


def main():
    cap = Capability.model_validate_json(SRC.read_text(encoding="utf-8"))
    reviewed = cap.bump_version()  # v1 -> v2

    fixed = False
    for step in reviewed.steps:
        if step.action.value == "extract" and step.output_name == "savings_balance":
            step.locator = Locator(
                strategies=[{"strategy": "css", "value": "p b"}],
                description="bolded balance amount inside 'Current Savings Balance: $X' paragraph",
            )
            step.description = "Extract savings_balance from the balance paragraph"
            fixed = True
    assert fixed, "expected to find the savings_balance extract step"

    reviewed.provenance.reviewed = True
    reviewed.provenance.reviewer = "human-review (see scripts/review_lookup_balance.py)"
    reviewed.approval_state = "approved"

    DST.write_text(json.dumps(reviewed.model_dump(), indent=2, default=str), encoding="utf-8")
    print(f"Wrote reviewed, approved capability: {DST}")


if __name__ == "__main__":
    main()
