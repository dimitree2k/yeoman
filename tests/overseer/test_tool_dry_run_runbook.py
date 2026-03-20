# tests/overseer/test_tool_dry_run_runbook.py
from __future__ import annotations
from pathlib import Path
from unittest.mock import MagicMock
import pytest
from yeoman_overseer.agent.tools.dry_run_runbook import dry_run_runbook

_VALID_RUNBOOK = """\
---
name: health-check
domain: health
trigger:
  kind: cron
  expr: "0 * * * *"
escalate_to_llm: false
---
## Actions
- action: noop
  target: system
"""

_INVALID_FRONTMATTER = """\
---
domain: health
trigger:
  kind: cron
---
"""

_UNKNOWN_TRIGGER = """\
---
name: bad-trigger
domain: health
trigger:
  kind: webhook
---
"""


def _ctx() -> MagicMock:
    return MagicMock()


def test_valid_runbook_returns_valid_true(tmp_path):
    rb_path = tmp_path / "health-check.md"
    rb_path.write_text(_VALID_RUNBOOK)
    result = dry_run_runbook(str(rb_path), ctx=_ctx())
    assert result["valid"] is True
    assert result["issues"] == []


def test_invalid_frontmatter_reports_issues(tmp_path):
    rb_path = tmp_path / "bad.md"
    rb_path.write_text(_INVALID_FRONTMATTER)
    result = dry_run_runbook(str(rb_path), ctx=_ctx())
    assert result["valid"] is False
    assert len(result["issues"]) > 0


def test_missing_file_reports_issue(tmp_path):
    result = dry_run_runbook(str(tmp_path / "missing.md"), ctx=_ctx())
    assert result["valid"] is False
    assert any("not found" in i.lower() for i in result["issues"])


def test_action_plan_extracted(tmp_path):
    rb_path = tmp_path / "rb.md"
    rb_path.write_text(_VALID_RUNBOOK)
    result = dry_run_runbook(str(rb_path), ctx=_ctx())
    assert isinstance(result["action_plan"], list)


def test_unknown_trigger_kind_reports_issue(tmp_path):
    rb_path = tmp_path / "webhook.md"
    rb_path.write_text(_UNKNOWN_TRIGGER)
    result = dry_run_runbook(str(rb_path), ctx=_ctx())
    assert result["valid"] is False
