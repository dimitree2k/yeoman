"""Tests for causal chain detection."""
from __future__ import annotations
from yeoman_overseer.safety.causal import CausalChainDetector

def test_no_cycle_single_action() -> None:
    cd = CausalChainDetector()
    cd.record_action("A", triggered_by=None, state_changed=[], state_read=[])
    assert cd.detect_cycle() is None

def test_direct_cycle_detected() -> None:
    cd = CausalChainDetector()
    cd.record_action("A", triggered_by=None, state_changed=[], state_read=[])
    cd.record_action("B", triggered_by="A", state_changed=[], state_read=[])
    cd.record_action("A", triggered_by="B", state_changed=[], state_read=[])
    cycle = cd.detect_cycle()
    assert cycle is not None
    assert set(cycle) == {"A", "B"}

def test_state_mediated_cycle_detected() -> None:
    cd = CausalChainDetector()
    cd.record_action("A", triggered_by=None, state_changed=["memory.db"], state_read=[])
    cd.record_action("B", triggered_by=None, state_changed=["gateway.log"], state_read=["memory.db"])
    cd.record_action("A", triggered_by=None, state_changed=["memory.db"], state_read=["gateway.log"])
    cycle = cd.detect_cycle()
    assert cycle is not None
    assert set(cycle) == {"A", "B"}

def test_max_chain_depth() -> None:
    cd = CausalChainDetector(max_depth=3)
    cd.record_action("A", triggered_by=None, state_changed=[], state_read=[])
    cd.record_action("B", triggered_by="A", state_changed=[], state_read=[])
    cd.record_action("C", triggered_by="B", state_changed=[], state_read=[])
    cd.record_action("A", triggered_by="C", state_changed=[], state_read=[])
    cycle = cd.detect_cycle()
    assert cycle is not None

def test_no_false_positive_independent_actions() -> None:
    cd = CausalChainDetector()
    cd.record_action("A", triggered_by=None, state_changed=["file_a"], state_read=[])
    cd.record_action("B", triggered_by=None, state_changed=["file_b"], state_read=[])
    assert cd.detect_cycle() is None

def test_clear_window() -> None:
    cd = CausalChainDetector()
    cd.record_action("A", triggered_by=None, state_changed=[], state_read=[])
    cd.record_action("B", triggered_by="A", state_changed=[], state_read=[])
    cd.clear()
    cd.record_action("A", triggered_by="B", state_changed=[], state_read=[])
    assert cd.detect_cycle() is None
