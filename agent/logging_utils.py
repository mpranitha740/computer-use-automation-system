"""Structured JSONL evidence logging shared by discovery, replay, and escalation.

All log records go through `redact_mapping` before being written, so raw
sensitive values (credentials, full PII) never reach disk even if a caller
forgets to mark a field -- key-name-based redaction is a backstop under the
schema-based `sensitive` flags.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from agent.guardrails import redact_mapping


class RunLogger:
    def __init__(self, evidence_dir: Path, run_id: str, kind: str):
        self.run_id = run_id
        self.kind = kind
        self.dir = evidence_dir / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.dir / "run.log.jsonl"
        self._fh = self.log_path.open("a", encoding="utf-8")

    def log(self, actor: str, event: str, **detail: Any) -> None:
        record = {
            "ts": time.time(),
            "run_id": self.run_id,
            "kind": self.kind,
            "actor": actor,
            "event": event,
            "detail": redact_mapping(detail, sensitive_keys={"password", "value"} if event == "fill_sensitive" else set()),
        }
        self._fh.write(json.dumps(record, default=str) + "\n")
        self._fh.flush()

    def screenshot_path(self, tag: str) -> Path:
        return self.dir / f"{tag}.png"

    def save_json(self, name: str, data: dict) -> Path:
        p = self.dir / name
        p.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        return p

    def close(self) -> None:
        self._fh.close()
