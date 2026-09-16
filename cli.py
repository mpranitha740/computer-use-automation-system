"""
Command-line entry point.

  python cli.py discover lookup_member_balance
  python cli.py discover open_sub_account_reach_confirmation
  python cli.py replay artifacts/lookup_member_balance.v1.json member_id=12345
  python cli.py replay artifacts/lookup_member_balance.v1.json member_id=00000
  python cli.py replay artifacts/lookup_member_balance.v1.json member_id=99999
  python cli.py list

See README.md for full setup and the exact demo path.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (no external dependency): KEY=VALUE per line,
    '#' comments, blank lines ignored. Never overrides an already-set env var."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip()


_load_dotenv(Path(__file__).parent / ".env")

from agent.guardrails import AllowlistConfig
from agent.llm_agent import DiscoveryAgent
from agent.replay import replay as run_replay
from agent.schemas import Capability
from agent.session import MockBankSessionProvider
import scenarios

ROOT = Path(__file__).parent
ARTIFACTS_DIR = ROOT / "artifacts"
EVIDENCE_DIR = ROOT / "evidence"

SCENARIOS = {
    "lookup_member_balance": scenarios.LOOKUP_MEMBER_BALANCE,
    "open_sub_account_reach_confirmation": scenarios.OPEN_SUB_ACCOUNT_REACH_CONFIRMATION,
}


def cmd_discover(args):
    scenario = SCENARIOS.get(args.scenario)
    if scenario is None:
        print(f"unknown scenario {args.scenario!r}; choices: {list(SCENARIOS)}", file=sys.stderr)
        sys.exit(1)
    allowlist = AllowlistConfig()
    agent = DiscoveryAgent(allowlist=allowlist, evidence_dir=EVIDENCE_DIR)
    print(f"Running discovery for scenario {args.scenario!r} (headless={not args.headed}, "
          f"provider={agent.provider_name}, model={agent.model}) ...")
    capability, run_id, evidence_path = agent.run(
        max_steps=args.max_steps, headless=not args.headed, **scenario,
    )
    ARTIFACTS_DIR.mkdir(exist_ok=True)
    out_path = ARTIFACTS_DIR / f"{capability.name}.v{capability.version}.json"
    out_path.write_text(json.dumps(capability.model_dump(), indent=2, default=str), encoding="utf-8")
    print(f"Discovery SUCCEEDED. run_id={run_id}")
    print(f"Evidence written to: {evidence_path}")
    print(f"Artifact saved to:   {out_path}")


def _parse_kv(pairs: list[str]) -> dict:
    out = {}
    for p in pairs:
        if "=" not in p:
            print(f"bad param {p!r}, expected key=value", file=sys.stderr)
            sys.exit(1)
        k, v = p.split("=", 1)
        out[k] = v
    return out


def cmd_replay(args):
    artifact_path = Path(args.artifact)
    capability = Capability.model_validate_json(artifact_path.read_text(encoding="utf-8"))
    params = _parse_kv(args.params)
    # credentials come from env, never from the CLI, so they never land in shell history / logs
    params.setdefault("username", os.environ.get("MOCK_APP_USERNAME", "operator"))
    params.setdefault("password", os.environ.get("MOCK_APP_PASSWORD", "demo-pass"))
    allowlist = AllowlistConfig()
    session_provider = MockBankSessionProvider(scenarios.BASE_URL)
    escalation_commands = None
    if args.escalation_script:
        escalation_commands = json.loads(Path(args.escalation_script).read_text(encoding="utf-8"))
    print(f"Replaying {capability.name} v{capability.version} with params={ {k: ('***' if k=='password' else v) for k,v in params.items()} }")
    result = run_replay(
        capability, params, evidence_dir=EVIDENCE_DIR, allowlist=allowlist,
        session_provider=session_provider, escalation_commands=escalation_commands,
        headless=not args.headed,
    )
    print(json.dumps(result.model_dump(), indent=2, default=str))
    sys.exit(0 if result.status.value in ("success", "business_outcome") else 2)


def cmd_list(args):
    print("Scenarios (for `discover`):")
    for name in SCENARIOS:
        print(f"  - {name}")
    print("\nSaved artifacts (for `replay`):")
    if ARTIFACTS_DIR.exists():
        for p in sorted(ARTIFACTS_DIR.glob("*.json")):
            print(f"  - {p}")


def main():
    parser = argparse.ArgumentParser(description="Computer-use automation system CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_discover = sub.add_parser("discover", help="Run an LLM-driven discovery run and save the resulting capability")
    p_discover.add_argument("scenario", choices=list(SCENARIOS))
    p_discover.add_argument("--headed", action="store_true", help="show the browser window")
    p_discover.add_argument("--max-steps", type=int, default=20)
    p_discover.set_defaults(func=cmd_discover)

    p_replay = sub.add_parser("replay", help="Deterministically replay a saved capability artifact")
    p_replay.add_argument("artifact", help="path to a saved capability JSON file")
    p_replay.add_argument("params", nargs="*", help="key=value input parameters, e.g. member_id=12345")
    p_replay.add_argument("--headed", action="store_true")
    p_replay.add_argument("--escalation-script", help="JSON file of scripted operator commands, for non-interactive escalation demos")
    p_replay.set_defaults(func=cmd_replay)

    p_list = sub.add_parser("list", help="List available scenarios and saved artifacts")
    p_list.set_defaults(func=cmd_list)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
