"""
Human-in-the-loop escalation and control handoff.

Design (see REPORT.md section 5 for the full reasoning):

- Automation and the human operator share one `control_owner` state
  ("agent"/"replay" vs "human") attached to the run, and one live Playwright
  `page` object -- the human is never handed a fresh session, they take over
  the exact browser context (cookies, current URL, in-progress form state)
  the automation was using.
- `raise_intervention()` is the single entry point both the discovery loop
  and the replay engine call when they decide they cannot safely proceed. It
  captures context (goal/capability, step, screenshot, reason), persists an
  InterventionRequest, and flips control_owner to "human".
- `run_operator_console()` is the human side of the seam: it operates the
  *same* `page`. In a real deployment this would be a websocket-driven
  operator UI (out of scope per the brief); here it's a small command
  console that can be driven interactively (stdin) or by a scripted list of
  commands (used to produce reproducible /evidence/ output). Both paths run
  through the exact same action functions as automation does, and every
  human action is logged with actor="human" in the same run log automation
  writes to -- so the evidence trail is one continuous timeline, not two.
- `resume()` flips control back and lets the caller (replay engine) continue
  from the same session, re-snapshotting state rather than assuming nothing
  changed.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Literal, Optional

from playwright.sync_api import Page

from agent import actions
from agent.logging_utils import RunLogger
from agent.schemas import InterventionRequest, Locator

ControlOwner = Literal["agent", "replay", "human"]


class EscalationSession:
    def __init__(self, page: Page, logger: RunLogger, run_id: str):
        self.page = page
        self.logger = logger
        self.run_id = run_id
        self.control_owner: ControlOwner = "agent"

    def raise_intervention(
        self,
        *,
        reason: str,
        capability_id: Optional[str] = None,
        goal: Optional[str] = None,
        step_id: Optional[str] = None,
    ) -> InterventionRequest:
        shot = self.logger.screenshot_path(f"intervention_{step_id or 'na'}")
        try:
            self.page.screenshot(path=str(shot))
            shot_path = str(shot)
        except Exception:
            shot_path = None
        req = InterventionRequest(
            run_id=self.run_id,
            capability_id=capability_id,
            goal=goal,
            step_id=step_id,
            reason=reason,
            url=self.page.url,
            title=self.page.title(),
            screenshot_path=shot_path,
            status="pending",
        )
        self.logger.save_json(f"intervention_{req.id}.json", req.model_dump())
        self.logger.log("system", "intervention_raised", reason=reason, step_id=step_id,
                         intervention_id=req.id, url=req.url)
        self.control_owner = "human"
        return req

    def run_operator_console(
        self,
        request: InterventionRequest,
        commands: Optional[list[dict]] = None,
        input_fn: Callable[[str], str] = input,
    ) -> InterventionRequest:
        """Hand the live session to a human operator.

        If `commands` is given, replay that scripted list (used to produce
        deterministic /evidence/ for this submission and by tests). Otherwise
        read commands interactively from stdin -- this is the real,
        un-mocked path a human operator would use.

        Recognized commands: click <ref>, fill <ref> <text...>, select <ref> <text...>,
        note <text...>, resume, abort.
        """
        request.status = "in_progress"
        self.logger.log("human", "operator_attached", intervention_id=request.id, url=self.page.url)

        def handle(line: str) -> bool:
            """Returns False to stop the console loop."""
            line = line.strip()
            if not line:
                return True
            parts = line.split(maxsplit=2)
            cmd = parts[0].lower()
            if cmd == "resume":
                return False
            if cmd == "abort":
                request.status = "aborted"
                return False
            if cmd == "note" and len(parts) > 1:
                note = line.split(maxsplit=1)[1]
                request.human_actions.append({"action": "note", "text": note})
                self.logger.log("human", "note", text=note)
                return True
            if cmd in ("click", "fill", "select") and len(parts) >= 2:
                ref = parts[1]
                text_arg = parts[2] if len(parts) > 2 else None
                elements, url, title, _ = _snapshot_safe(self.page)
                match = next((e for e in elements if e.ref == ref), None)
                target_name = match.name if match else ref
                loc = Locator(
                    strategies=[{"strategy": "role", "role": match.role if match else "button",
                                 "value": target_name}],
                    description=f"human-selected element {ref} ({target_name})",
                )
                try:
                    if cmd == "click":
                        actions.do_click(self.page, loc)
                    elif cmd == "fill":
                        actions.do_fill(self.page, loc, text_arg or "")
                    elif cmd == "select":
                        actions.do_select_option(self.page, loc, text_arg or "")
                    request.human_actions.append(
                        {"action": cmd, "ref": ref, "target": target_name, "value": text_arg}
                    )
                    self.logger.log("human", f"manual_{cmd}", ref=ref, target=target_name,
                                     value=text_arg if cmd == "click" else "[recorded]")
                except Exception as exc:  # noqa: BLE001
                    self.logger.log("human", "manual_action_failed", ref=ref, error=str(exc))
                return True
            self.logger.log("human", "unrecognized_command", line=line)
            return True

        if commands is not None:
            for c in commands:
                keep_going = handle(c["line"])
                if not keep_going:
                    break
        else:
            print(f"\n[HUMAN INTERVENTION] {request.reason}")
            print(f"URL: {request.url}\nCommands: click <ref> | fill <ref> <text> | select <ref> <text> | note <text> | resume | abort\n")
            while True:
                line = input_fn("operator> ")
                if not handle(line):
                    break

        if request.status != "aborted":
            request.status = "resolved"
        import time as _time
        request.resolved_at = _time.time()
        self.logger.save_json(f"intervention_{request.id}.json", request.model_dump())
        self.logger.log("human", "operator_detached", intervention_id=request.id,
                         status=request.status, actions=len(request.human_actions))
        return request

    def resume(self, *, owner: ControlOwner = "replay") -> None:
        self.control_owner = owner
        self.logger.log("system", "control_resumed", owner=owner)


def _snapshot_safe(page: Page):
    from agent.perception import snapshot
    return snapshot(page)
