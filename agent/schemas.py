"""
Typed data model for the whole system:

- Capability: the versioned, reviewable, agent-invocable artifact produced by a
  discovery run and consumed by deterministic replay.
- ReplayResult: the structured result contract replay returns to a caller.
- RunLog / StepLog: the evidence trail for both discovery and replay runs.
- InterventionRequest: the human-escalation record.

Everything here is plain pydantic so it serializes to reviewable JSON and
validates on load (an artifact loaded from disk that no longer matches the
schema fails loudly instead of silently misbehaving on replay).
"""
from __future__ import annotations

import enum
import time
import uuid
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


# --------------------------------------------------------------------------
# Locators: how a step finds the control it acts on.
# --------------------------------------------------------------------------

class LocatorStrategy(str, enum.Enum):
    ROLE = "role"           # accessibility role + accessible name (most stable)
    TEXT = "text"           # visible text match
    LABEL = "label"         # form label text
    CSS = "css"             # CSS selector (least stable, last resort)
    XPATH = "xpath"


class Locator(BaseModel):
    """A ranked fallback chain for finding one control.

    Ordered strongest-to-weakest. Replay tries each in order and records which
    one actually matched, so drift shows up as "fell back to strategy N" in
    logs rather than as a silent success or a mystery failure.
    """
    strategies: list[dict[str, str]] = Field(
        description="Ordered list of {strategy, value[, role]} dicts, strongest first. "
                    "e.g. [{'strategy':'role','role':'button','value':'Search'}, "
                    "{'strategy':'text','value':'Search'}]"
    )
    description: str = Field(description="Human-readable description of the target control")


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------

class ActionType(str, enum.Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    FILL = "fill"
    SELECT_OPTION = "select_option"
    EXTRACT = "extract"          # read text into a named output
    ASSERT_TEXT = "assert_text"  # checkpoint-only, no state change


class RiskLevel(str, enum.Enum):
    SAFE_REVERSIBLE = "safe_reversible"
    RISKY_IRREVERSIBLE = "risky_irreversible"


class Checkpoint(BaseModel):
    """A post-condition asserted after a step to confirm the action actually
    landed, rather than assuming the click worked."""
    description: str
    locator: Optional[Locator] = None
    expect_text_contains: Optional[str] = None
    expect_url_contains: Optional[str] = None


class Step(BaseModel):
    id: str
    description: str
    action: ActionType
    locator: Optional[Locator] = None
    value_template: Optional[str] = Field(
        default=None,
        description="Literal value or {{param_name}} template substituted from input params "
                    "at replay time. Used for fill/select_option/navigate.",
    )
    output_name: Optional[str] = Field(
        default=None, description="For EXTRACT steps: which declared output this fills."
    )
    risk_level: RiskLevel = RiskLevel.SAFE_REVERSIBLE
    requires_human_approval: bool = Field(
        default=False,
        description="If true, replay MUST NOT execute this step unattended; it raises an "
                    "intervention request and waits for a human to perform it on the live "
                    "session before continuing.",
    )
    checkpoint: Optional[Checkpoint] = None
    timeout_ms: int = 5000


# --------------------------------------------------------------------------
# Parameters / outputs (the capability's typed contract)
# --------------------------------------------------------------------------

class ParamType(str, enum.Enum):
    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"


class Parameter(BaseModel):
    name: str
    type: ParamType
    required: bool = True
    description: str
    example: Optional[str] = None
    sensitive: bool = Field(
        default=False,
        description="If true, this value is redacted in logs/evidence and never written "
                    "into a saved artifact's example/default fields.",
    )


class OutputSpec(BaseModel):
    name: str
    type: ParamType
    description: str
    sensitive: bool = False


# --------------------------------------------------------------------------
# Business outcome taxonomy (declared per-capability so replay knows what a
# *legitimate* non-success answer looks like for this specific flow).
# --------------------------------------------------------------------------

class BusinessOutcomePattern(BaseModel):
    code: str = Field(description="stable machine-readable outcome code, e.g. 'member_not_found'")
    description: str
    match_text_contains: str = Field(description="substring match against visible page text")


# --------------------------------------------------------------------------
# The capability artifact itself
# --------------------------------------------------------------------------

class TargetApp(BaseModel):
    app_id: str
    vendor_product: str = Field(description="logical product name, for cross-tenant reuse matching")
    base_url: str
    tenant_id: Optional[str] = Field(
        default=None, description="null = tenant-agnostic base recording; set on a per-tenant override"
    )


class Provenance(BaseModel):
    discovered_at: float
    discovered_by_model: str
    source_run_id: str
    reviewed: bool = False
    reviewer: Optional[str] = None


class Capability(BaseModel):
    """The reusable, agent-invocable artifact. This is the contract an AI
    agent calls: name + parameters in, typed outputs (or a declared business
    outcome) back, independent of the raw LLM transcript that discovered it.
    """
    capability_id: str = Field(default_factory=lambda: _new_id("cap"))
    name: str
    version: int = 1
    description: str
    target: TargetApp
    parameters: list[Parameter] = Field(default_factory=list)
    outputs: list[OutputSpec] = Field(default_factory=list)
    steps: list[Step]
    success_checkpoint: Checkpoint
    business_outcomes: list[BusinessOutcomePattern] = Field(default_factory=list)
    approval_state: Literal["draft", "approved"] = "draft"
    provenance: Provenance

    def bump_version(self) -> "Capability":
        new = self.model_copy(deep=True)
        new.version += 1
        return new


# --------------------------------------------------------------------------
# Replay result contract
# --------------------------------------------------------------------------

class ReplayStatus(str, enum.Enum):
    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"
    HARD_FAILURE = "hard_failure"
    AWAITING_HUMAN = "awaiting_human"


class FailureDetail(BaseModel):
    step_id: str
    step_description: str
    expected: str
    observed: str
    screenshot_path: Optional[str] = None


class ReplayResult(BaseModel):
    status: ReplayStatus
    capability_id: str
    capability_version: int
    run_id: str
    outputs: dict[str, Any] = Field(default_factory=dict)
    business_outcome_code: Optional[str] = None
    business_outcome_message: Optional[str] = None
    failure: Optional[FailureDetail] = None
    intervention_request_id: Optional[str] = None
    duration_ms: int = 0


# --------------------------------------------------------------------------
# Evidence / logging
# --------------------------------------------------------------------------

class StepLog(BaseModel):
    ts: float
    run_id: str
    actor: Literal["agent", "replay", "human"]
    step_index: int
    kind: str
    detail: dict[str, Any] = Field(default_factory=dict)


class InterventionRequest(BaseModel):
    id: str = Field(default_factory=lambda: _new_id("intervent"))
    created_at: float = Field(default_factory=time.time)
    run_id: str
    capability_id: Optional[str] = None
    goal: Optional[str] = None
    step_id: Optional[str] = None
    reason: str
    url: Optional[str] = None
    title: Optional[str] = None
    screenshot_path: Optional[str] = None
    status: Literal["pending", "in_progress", "resolved", "aborted"] = "pending"
    resolved_at: Optional[float] = None
    human_actions: list[dict[str, Any]] = Field(default_factory=list)
