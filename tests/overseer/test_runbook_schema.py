"""Tests for runbook schema validation."""
from __future__ import annotations
import pytest
from yeoman_overseer.runbook.schema import RunbookFrontmatter, TriggerConfig, TriggerCondition, SafetyConfig, LLMBudget

def test_minimal_poll_runbook() -> None:
    fm = RunbookFrontmatter(name="test-health", domain="health", trigger=TriggerConfig(kind="poll", interval_s=30, condition=TriggerCondition(check="process_alive", target="yeoman-gateway", operator="==", value=True)))
    assert fm.name == "test-health"
    assert fm.enabled is True
    assert fm.escalate_to_llm is False
    assert fm.trigger.kind == "poll"
    assert fm.trigger.interval_s == 30
    assert fm.safety.max_actions_per_hour == 10

def test_cron_trigger_requires_expr() -> None:
    with pytest.raises(ValueError, match="cron.*requires.*expr"):
        TriggerConfig(kind="cron")

def test_poll_trigger_requires_interval_and_condition() -> None:
    with pytest.raises(ValueError, match="poll.*requires.*interval_s"):
        TriggerConfig(kind="poll")

def test_event_trigger_requires_event_name() -> None:
    with pytest.raises(ValueError, match="event.*requires.*event_name"):
        TriggerConfig(kind="event")

def test_safety_defaults() -> None:
    s = SafetyConfig()
    assert s.max_actions_per_hour == 10
    assert s.rollback is True
    assert s.cooldown_s == 300
    assert s.requires_tests is False
    assert s.on_lock_conflict == "skip"

def test_llm_budget_defaults() -> None:
    fm = RunbookFrontmatter(name="test", domain="evolution", trigger=TriggerConfig(kind="cron", expr="0 3 * * 0"), escalate_to_llm=True, llm_budget=LLMBudget())
    assert fm.escalate_to_llm is True
    assert fm.llm_budget is not None
    assert fm.llm_budget.max_tokens == 30_000
    assert fm.llm_budget.max_tool_calls == 100
    assert fm.llm_budget.llm_profile == "overseerDefault"

def test_operator_validation() -> None:
    c = TriggerCondition(check="disk_usage_above", target="/home", operator=">=", value=80)
    assert c.operator == ">="
    with pytest.raises(ValueError):
        TriggerCondition(check="disk_usage_above", target="/home", operator="LIKE", value=80)

def test_origin_defaults_to_manual() -> None:
    fm = RunbookFrontmatter(name="test", domain="health", trigger=TriggerConfig(kind="cron", expr="0 * * * *"))
    assert fm.origin == "manual"
