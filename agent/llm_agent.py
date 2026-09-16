"""
Discovery: an LLM-driven observe -> decide -> act loop against a live
Playwright page. Produces a Capability artifact from a successful run.

Perception is the ElementRef/text snapshot from agent.perception (role +
accessible name, not raw markup) -- see that module's docstring for why.
Action is the shared actuator in agent.actions. Every proposed action is
checked against the guardrail allowlist and risk classifier *before* it is
executed; risky/irreversible controls are never auto-clicked by the agent,
discovery or replay, regardless of what the model decides.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Optional

from playwright.sync_api import sync_playwright

from agent import actions, perception
from agent.guardrails import AllowlistConfig, GuardrailViolation, check_allowlist, classify_control_risk
from agent.llm_providers import ToolCall, build_provider
from agent.logging_utils import RunLogger
from agent.schemas import (
    ActionType, Capability, Checkpoint, Locator, OutputSpec, Parameter,
    Provenance, RiskLevel, Step, TargetApp,
)

DEFAULT_PROVIDER = os.environ.get("LLM_PROVIDER", "anthropic")
DEFAULT_MODEL = os.environ.get("LLM_MODEL", "claude-sonnet-5" if DEFAULT_PROVIDER == "anthropic" else "gemini-2.5-flash")

TOOLS = [
    {
        "name": "navigate",
        "description": "Go to an absolute URL within the target application.",
        "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
    },
    {
        "name": "click",
        "description": "Click the element with the given ref from the latest snapshot.",
        "input_schema": {"type": "object", "properties": {"ref": {"type": "string"}}, "required": ["ref"]},
    },
    {
        "name": "fill",
        "description": "Type text into the text input/textarea with the given ref, replacing its content.",
        "input_schema": {
            "type": "object",
            "properties": {"ref": {"type": "string"}, "value": {"type": "string"}},
            "required": ["ref", "value"],
        },
    },
    {
        "name": "select_option",
        "description": "Choose an option (by its visible label) in the <select> with the given ref.",
        "input_schema": {
            "type": "object",
            "properties": {"ref": {"type": "string"}, "value": {"type": "string"}},
            "required": ["ref", "value"],
        },
    },
    {
        "name": "extract",
        "description": "Read the visible text of the element with the given ref and record it under output_name. "
                        "output_name must be one of the declared outputs for this goal.",
        "input_schema": {
            "type": "object",
            "properties": {"ref": {"type": "string"}, "output_name": {"type": "string"}},
            "required": ["ref", "output_name"],
        },
    },
    {
        "name": "finish",
        "description": "Call this once the goal has been reached (all declared outputs extracted, and/or the "
                        "success checkpoint state is visible), or if you are stuck and cannot safely proceed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["success", "stuck"]},
                "reason": {"type": "string"},
            },
            "required": ["status", "reason"],
        },
    },
]


class DiscoveryStuck(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _system_prompt(goal: str, parameters: list[Parameter], outputs: list[OutputSpec],
                    success_hint: str) -> str:
    param_lines = "\n".join(f"  - {p.name} ({p.type.value}): use the literal value {p.example!r}" for p in parameters)
    output_lines = "\n".join(f"  - {o.name} ({o.type.value}): {o.description}" for o in outputs)
    return f"""You are operating a legacy, server-rendered internal bank admin console through a
constrained tool interface. You do not see raw HTML; each turn you get a numbered list of
currently visible interactive elements (ref, role, accessible name) plus a text summary of the
page. Some elements may look unlabeled -- that's normal for this legacy app; infer purpose from
surrounding visible text and table structure.

GOAL: {goal}

Use exactly these literal parameter values when the goal calls for entering them:
{param_lines or '  (none)'}

You must extract these outputs via the `extract` tool before finishing successfully:
{output_lines or '  (none)'}

Success looks like: {success_hint}

Rules:
- Only act on refs that appeared in the most recent snapshot.
- Some controls are irreversible (e.g. anything that finally submits/confirms/deletes/approves
  a transaction). Do NOT click those. If the goal is satisfied by simply reaching the screen
  that contains such a control (without clicking it), that counts as done -- call finish(success).
- If an action is rejected by policy, do not retry the same action; pick a different path or,
  if there is truly no safe way forward, call finish(status="stuck", reason=...).
- Call `finish` exactly once, when done or stuck.
"""


def _format_snapshot(elements, url, title, text_summary) -> str:
    lines = [f"URL: {url}", f"TITLE: {title}", "", "VISIBLE TEXT:", text_summary, "", "INTERACTIVE ELEMENTS:"]
    for el in elements:
        lines.append(f"  [{el.ref}] role={el.role} name={el.name!r} tag={el.tag}"
                      + (f" value={el.current_value!r}" if el.current_value else ""))
    return "\n".join(lines)


class DiscoveryAgent:
    def __init__(self, allowlist: AllowlistConfig, evidence_dir: Path,
                 model: str = DEFAULT_MODEL, provider_name: str = DEFAULT_PROVIDER):
        self.model = model
        self.provider_name = provider_name
        self.allowlist = allowlist
        self.evidence_dir = evidence_dir

    def run(
        self,
        *,
        capability_name: str,
        goal: str,
        target: TargetApp,
        parameters: list[Parameter],
        outputs: list[OutputSpec],
        success_hint: str,
        success_checkpoint: Checkpoint,
        business_outcomes: Optional[list] = None,
        max_steps: int = 20,
        headless: bool = True,
    ) -> tuple[Capability, str, Path]:
        run_id = f"discover_{uuid.uuid4().hex[:8]}"
        logger = RunLogger(self.evidence_dir, run_id, kind="discovery")
        logger.log("system", "run_start", goal=goal, capability_name=capability_name,
                    target=target.model_dump(), parameters=[p.model_dump() for p in parameters])

        recorded_steps: list[Step] = []
        extracted_outputs: dict[str, str] = {}
        known_values = {p.name: p.example for p in parameters if p.example}
        provider = build_provider(self.provider_name, self.model)
        system_prompt = _system_prompt(goal, parameters, outputs, success_hint)

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=headless)
            context = browser.new_context()
            page = context.new_page()
            check_allowlist(self.allowlist, url=target.base_url, action_type=ActionType.NAVIGATE)
            actions.do_navigate(page, target.base_url)
            recorded_steps.append(Step(
                id="step_0", description=f"Navigate to {target.base_url}",
                action=ActionType.NAVIGATE, value_template=target.base_url,
            ))
            logger.log("agent", "navigate", url=target.base_url)

            status = "stuck"
            reason = "max_steps exceeded"
            try:
                for step_num in range(1, max_steps + 1):
                    elements, url, title, text_summary = perception.snapshot(page)
                    snap_text = _format_snapshot(elements, url, title, text_summary)

                    tool_call = provider.observe(system_prompt, snap_text, TOOLS)
                    if tool_call and tool_call.text:
                        logger.log("agent", "reasoning", text=tool_call.text[:2000])

                    if tool_call is None:
                        continue

                    logger.log("agent", "tool_call", tool=tool_call.name, input=tool_call.input, step=step_num)

                    if tool_call.name == "finish":
                        status = tool_call.input.get("status", "stuck")
                        reason = tool_call.input.get("reason", "")
                        break

                    tool_result_text, step = self._execute_tool(
                        page, tool_call.name, tool_call.input, elements, step_num,
                        known_values, extracted_outputs, logger,
                    )
                    if step is not None:
                        recorded_steps.append(step)
                    provider.report_result(tool_call, tool_result_text)
                else:
                    status, reason = "stuck", "max_steps exceeded without finish"
            except Exception as exc:  # noqa: BLE001
                status, reason = "stuck", f"unhandled error: {exc}"
                shot = logger.screenshot_path("discovery_error")
                try:
                    page.screenshot(path=str(shot))
                except Exception:
                    pass

            final_shot = logger.screenshot_path("final_state")
            try:
                page.screenshot(path=str(final_shot), full_page=True)
            except Exception:
                pass
            browser.close()

        logger.log("system", "run_end", status=status, reason=reason,
                    outputs=extracted_outputs, steps=len(recorded_steps))
        logger.close()

        if status != "success":
            raise DiscoveryStuck(reason)

        capability = self._build_capability(
            capability_name, goal, target, parameters, outputs, recorded_steps,
            success_checkpoint, business_outcomes or [], run_id,
        )
        logger.save_json("artifact.json", capability.model_dump())
        return capability, run_id, logger.dir

    def _execute_tool(self, page, name, tool_input, elements, step_num, known_values,
                       extracted_outputs, logger):
        ref = tool_input.get("ref")
        el = next((e for e in elements if e.ref == ref), None) if ref else None
        step_id = f"step_{step_num}"

        try:
            if name == "navigate":
                url = tool_input["url"]
                check_allowlist(self.allowlist, url=url, action_type=ActionType.NAVIGATE)
                actions.do_navigate(page, url)
                logger.log("agent", "navigate", url=url)
                return "ok: navigated", Step(id=step_id, description=f"Navigate to {url}",
                                              action=ActionType.NAVIGATE, value_template=url)

            if el is None:
                return f"error: no element with ref {ref!r} in latest snapshot", None

            risk = classify_control_risk(self.allowlist, accessible_name=el.name)
            if risk == RiskLevel.RISKY_IRREVERSIBLE:
                logger.log("system", "guardrail_blocked", ref=ref, name=el.name, step=step_num)
                return (f"BLOCKED by policy: {el.name!r} is classified risky/irreversible and cannot "
                        f"be auto-executed. Do not retry it; either finish if the goal is already "
                        f"satisfied, or find a different path."), None

            check_allowlist(self.allowlist, url=page.url, action_type=ActionType(name))
            locator = perception.build_locator(el)

            if name == "click":
                actions.do_click(page, locator)
                logger.log("agent", "click", ref=ref, name=el.name)
                return "ok: clicked", Step(id=step_id, description=f"Click {el.role} {el.name!r}",
                                            action=ActionType.CLICK, locator=locator,
                                            risk_level=risk)

            if name == "fill":
                value = tool_input["value"]
                template = _templatize(value, known_values)
                actions.do_fill(page, locator, value)
                logger.log("agent", "fill", ref=ref, name=el.name, value_template=template)
                return "ok: filled", Step(id=step_id, description=f"Fill {el.role} {el.name!r}",
                                           action=ActionType.FILL, locator=locator,
                                           value_template=template, risk_level=risk)

            if name == "select_option":
                value = tool_input["value"]
                actions.do_select_option(page, locator, value)
                logger.log("agent", "select_option", ref=ref, name=el.name, value=value)
                return "ok: selected", Step(id=step_id, description=f"Select {value!r} in {el.name!r}",
                                             action=ActionType.SELECT_OPTION, locator=locator,
                                             value_template=value, risk_level=risk)

            if name == "extract":
                text, _ = actions.do_extract(page, locator)
                output_name = tool_input["output_name"]
                extracted_outputs[output_name] = text
                logger.log("agent", "extract", ref=ref, output_name=output_name, value=text)
                return f"ok: extracted {text!r}", Step(
                    id=step_id, description=f"Extract {output_name} from {el.name!r}",
                    action=ActionType.EXTRACT, locator=locator, output_name=output_name, risk_level=risk,
                )

            return f"error: unknown tool {name}", None
        except GuardrailViolation as exc:
            logger.log("system", "guardrail_blocked", tool=name, input=tool_input, error=str(exc))
            return f"BLOCKED by policy: {exc}", None
        except actions.ElementNotFound as exc:
            logger.log("agent", "element_not_found", ref=ref, tried=exc.tried)
            return f"error: could not resolve element {ref!r}: {exc.tried}", None
        except Exception as exc:  # noqa: BLE001
            logger.log("agent", "action_error", tool=name, error=str(exc))
            return f"error: {exc}", None

    def _build_capability(self, name, goal, target, parameters, outputs, steps,
                           success_checkpoint, business_outcomes, run_id) -> Capability:
        return Capability(
            name=name,
            description=goal,
            target=target,
            parameters=[Parameter(**{**p.model_dump(), "example": (None if p.sensitive else p.example)})
                        for p in parameters],
            outputs=outputs,
            steps=steps,
            success_checkpoint=success_checkpoint,
            business_outcomes=business_outcomes,
            provenance=Provenance(discovered_at=time.time(), discovered_by_model=self.model, source_run_id=run_id),
        )


def _templatize(value: str, known_values: dict[str, str]) -> str:
    for name, known in known_values.items():
        if known and known in value:
            value = value.replace(known, f"{{{{{name}}}}}")
    return value
