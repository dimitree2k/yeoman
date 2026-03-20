"""Validate a runbook without executing it."""
from __future__ import annotations

import re
from pathlib import Path

from yeoman_overseer.runbook.parser import parse_runbook

_KNOWN_TRIGGER_KINDS = {"poll", "cron", "event"}
_ACTION_RE = re.compile(r"^[-*]\s+action:\s+(\w+)", re.MULTILINE)


def dry_run_runbook(path: str, *, ctx: object) -> dict:
    """Parse and validate a runbook file without executing any actions."""
    rb_path = Path(path)
    issues: list[str] = []

    if not rb_path.exists():
        return {
            "valid": False,
            "trigger_would_fire": False,
            "action_plan": [],
            "issues": ["file not found: " + path],
        }

    try:
        runbook = parse_runbook(rb_path)
    except Exception as exc:
        return {
            "valid": False,
            "trigger_would_fire": False,
            "action_plan": [],
            "issues": [f"parse error: {exc}"],
        }

    if runbook.meta.trigger.kind not in _KNOWN_TRIGGER_KINDS:
        issues.append(f"unknown trigger kind: {runbook.meta.trigger.kind!r}")

    action_plan = _ACTION_RE.findall(runbook.body or "")

    return {
        "valid": len(issues) == 0,
        "trigger_would_fire": False,
        "action_plan": action_plan,
        "issues": issues,
    }
