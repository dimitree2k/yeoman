"""Tests for LLMBudget schema extensions."""
from yeoman_overseer.runbook.schema import LLMBudget, RunbookFrontmatter


def test_llm_budget_new_defaults():
    b = LLMBudget()
    assert b.max_tokens == 30_000
    assert b.max_tool_calls == 100
    assert b.llm_profile == "overseerDefault"


def test_llm_budget_custom():
    b = LLMBudget(max_tokens=8000, max_tool_calls=20, llm_profile="overseerFast")
    assert b.llm_profile == "overseerFast"


def test_runbook_llm_budget_parses():
    import yaml

    raw = yaml.safe_load("""
name: test
domain: health
trigger:
  kind: cron
  expr: "0 * * * *"
escalate_to_llm: true
llm_budget:
  llm_profile: overseerFast
  max_tokens: 5000
""")
    fm = RunbookFrontmatter(**raw)
    assert fm.escalate_to_llm is True
    assert fm.llm_budget.llm_profile == "overseerFast"
    assert fm.llm_budget.max_tokens == 5000
