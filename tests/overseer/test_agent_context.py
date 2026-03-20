"""Tests for LLM agent context assembly."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from yeoman_overseer.agent.context import AgentContext, build_context
from yeoman_overseer.runbook.parser import Runbook
from yeoman_overseer.runbook.schema import LLMBudget, RunbookFrontmatter, TriggerConfig


def _runbook(domain: str = "memory") -> Runbook:
    meta = RunbookFrontmatter(
        name="test-runbook",
        domain=domain,
        trigger=TriggerConfig(kind="cron", expr="0 3 * * *"),
        escalate_to_llm=True,
        llm_budget=LLMBudget(),
    )
    return Runbook(meta=meta, body="## Instructions\nCheck memory.", path=Path("/tmp/test.md"))


def _audit(
    entries: list[dict] | None = None,
    tombstones: list[dict] | None = None,
) -> MagicMock:
    audit = MagicMock()
    audit.read_recent.return_value = entries or []
    audit.query_tombstones.return_value = tombstones or []
    return audit


def test_build_context_returns_agent_context() -> None:
    rb = _runbook()
    ctx = build_context(rb, {"disk_pct": 42}, _audit())
    assert isinstance(ctx, AgentContext)
    assert ctx.system_prompt
    assert ctx.user_message


def test_system_prompt_contains_identity() -> None:
    ctx = build_context(_runbook(), {}, _audit())
    assert "overseer" in ctx.system_prompt.lower()


def test_user_message_contains_runbook_name() -> None:
    ctx = build_context(_runbook(), {}, _audit())
    assert "test-runbook" in ctx.user_message


def test_user_message_contains_observations() -> None:
    ctx = build_context(_runbook(), {"error_rate": 0.05}, _audit())
    assert "error_rate" in ctx.user_message


def test_audit_log_filtered_by_domain() -> None:
    audit = _audit(entries=[
        {"domain": "memory", "runbook": "x", "action": "prune"},
        {"domain": "health", "runbook": "y", "action": "restart"},
    ])
    ctx = build_context(_runbook(domain="memory"), {}, audit)
    audit.read_recent.assert_called_once_with(limit=20, domain="memory")


def test_tombstones_filtered_by_domain() -> None:
    audit = _audit(tombstones=[{"name": "weather-skill", "domain": "memory"}])
    ctx = build_context(_runbook(domain="memory"), {}, audit)
    audit.query_tombstones.assert_called_once_with(domain="memory")
    assert "weather-skill" in ctx.user_message
