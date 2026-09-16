"""
Safety guardrails, enforced identically during discovery and replay.

Two independent gates:
  1. Allowlist  — is this domain/route/action-type permitted at all?
  2. Risk class — if permitted, is it safe/reversible or risky/irreversible?
     Risky actions are never auto-executed; they always require human
     approval, regardless of what the LLM (during discovery) or the artifact
     (during replay) says.

The allowlist is data (JSON/YAML-able), not code, so it can be configured per
tenant/app without touching the agent.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from agent.schemas import ActionType, RiskLevel


class GuardrailViolation(Exception):
    """Raised when an action falls outside the allowlist. Always fatal to the
    attempted action (never silently skipped) so it shows up in logs."""


@dataclass
class AllowlistConfig:
    allowed_domains: list[str] = field(default_factory=lambda: ["127.0.0.1", "localhost"])
    allowed_route_prefixes: list[str] = field(default_factory=lambda: ["/"])
    allowed_action_types: list[ActionType] = field(
        default_factory=lambda: [
            ActionType.NAVIGATE, ActionType.CLICK, ActionType.FILL,
            ActionType.SELECT_OPTION, ActionType.EXTRACT, ActionType.ASSERT_TEXT,
        ]
    )
    # Text patterns on the *control being acted on* that mark it irreversible
    # regardless of what step/route it lives on. This is deliberately
    # conservative: matched by substring against the element's accessible
    # name, case-insensitive.
    risky_control_text_patterns: list[str] = field(
        default_factory=lambda: [
            "confirm & open account",
            "confirm &amp; open account",
            "submit payment",
            "delete",
            "close account",
            "approve transfer",
        ]
    )


def check_allowlist(cfg: AllowlistConfig, *, url: str, action_type: ActionType) -> None:
    if action_type not in cfg.allowed_action_types:
        raise GuardrailViolation(f"action type {action_type} is not in the allowlist")
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if host not in cfg.allowed_domains:
        raise GuardrailViolation(f"domain {host!r} is not in the allowlist {cfg.allowed_domains}")
    if not any(parsed.path.startswith(p) for p in cfg.allowed_route_prefixes):
        raise GuardrailViolation(f"route {parsed.path!r} is not in the allowed route prefixes")


def classify_control_risk(cfg: AllowlistConfig, *, accessible_name: str) -> RiskLevel:
    name = (accessible_name or "").strip().lower()
    for pattern in cfg.risky_control_text_patterns:
        if pattern.lower() in name:
            return RiskLevel.RISKY_IRREVERSIBLE
    return RiskLevel.SAFE_REVERSIBLE


SENSITIVE_KEY_PATTERN = re.compile(
    r"password|passwd|secret|token|ssn|social.?security|credit.?card|cvv|pin\b", re.IGNORECASE
)


def redact(value: str, *, sensitive: bool) -> str:
    if not sensitive:
        return value
    if not value:
        return value
    if len(value) <= 4:
        return "*" * len(value)
    return value[:1] + "*" * (len(value) - 2) + value[-1]


def redact_mapping(data: dict, *, sensitive_keys: set[str] | None = None) -> dict:
    """Redact a dict for logging: values under keys that look sensitive by
    name, or that are explicitly flagged via sensitive_keys, are masked.
    Never raises on unknown shapes -- logging must never crash a run."""
    sensitive_keys = sensitive_keys or set()
    out = {}
    for k, v in data.items():
        is_sensitive = k in sensitive_keys or bool(SENSITIVE_KEY_PATTERN.search(k))
        if isinstance(v, str) and is_sensitive:
            out[k] = redact(v, sensitive=True)
        elif isinstance(v, dict):
            out[k] = redact_mapping(v, sensitive_keys=sensitive_keys)
        else:
            out[k] = v
    return out
