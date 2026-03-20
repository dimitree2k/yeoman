# tests/overseer/test_agent_budget.py
from datetime import date
from yeoman_overseer.agent.budget import BudgetTracker
from yeoman_overseer.state import OverseerState

def _tracker(calls_per_day=20, tokens_per_day=500_000):
    state = OverseerState()
    return BudgetTracker(state, calls_per_day=calls_per_day, tokens_per_day=tokens_per_day)

def test_can_call_llm_fresh():
    t = _tracker()
    assert t.can_call_llm("health") is True
    assert t.can_call_llm("memory") is True

def test_at_100_percent_blocks_all():
    t = _tracker(calls_per_day=1, tokens_per_day=100)
    t.consume(100, 1)
    assert t.can_call_llm("health") is False
    assert t.can_call_llm("memory") is False

def test_at_80_percent_blocks_non_health():
    t = _tracker(calls_per_day=10, tokens_per_day=1000)
    t.consume(800, 0)   # 80% of token budget
    assert t.can_call_llm("health") is True
    assert t.can_call_llm("memory") is False
    assert t.can_call_llm("governance") is False

def test_consume_persists_to_state():
    state = OverseerState()
    t = BudgetTracker(state, calls_per_day=20, tokens_per_day=500_000)
    t.consume(1500, 2)
    assert state.budget["tokens_daily"] == 1500
    assert state.budget["llm_daily"] == 2

def test_reset_on_new_day():
    state = OverseerState()
    state.budget["tokens_daily"] = 400_000
    state.budget["llm_daily"] = 15
    state.budget["budget_reset_date"] = "2020-01-01"   # old date
    t = BudgetTracker(state, calls_per_day=20, tokens_per_day=500_000)
    assert t.can_call_llm("memory") is True   # triggers reset
    assert state.budget["tokens_daily"] == 0
    assert state.budget["llm_daily"] == 0
    assert state.budget["budget_reset_date"] == date.today().isoformat()
