"""Tests for AuditEntry LLM fields and query_tombstones domain filter."""
import json
import tempfile
from pathlib import Path

from yeoman_overseer.audit.logger import AuditEntry, AuditLogger, TombstoneEntry


def test_audit_entry_llm_fields_optional():
    e = AuditEntry(
        runbook="test",
        trigger="cron",
        action="escalate",
        target="",
        result="success",
        duration_ms=100,
        escalated_to_llm=True,
        domain="memory",
    )
    assert e.llm_tokens_used is None
    assert e.llm_tool_calls is None
    assert e.llm_profile is None
    assert e.reasoning_summary is None


def test_audit_entry_llm_fields_set():
    e = AuditEntry(
        runbook="test",
        trigger="cron",
        action="escalate",
        target="",
        result="success",
        duration_ms=500,
        escalated_to_llm=True,
        domain="memory",
        llm_tokens_used=1200,
        llm_tool_calls=3,
        llm_profile="overseerDefault",
        reasoning_summary="pruned 5 stale entries",
    )
    assert e.llm_tokens_used == 1200


def test_audit_entry_llm_roundtrips_json():
    e = AuditEntry(
        runbook="x",
        trigger="cron",
        action="a",
        target="",
        result="ok",
        duration_ms=10,
        escalated_to_llm=True,
        domain="health",
        llm_tokens_used=500,
        llm_tool_calls=2,
        llm_profile="p",
        reasoning_summary="r",
    )
    with tempfile.TemporaryDirectory() as d:
        logger = AuditLogger(Path(d))
        record = logger.append(e)
        assert record["llm_tokens_used"] == 500
        assert record["reasoning_summary"] == "r"


def test_query_tombstones_domain_filter():
    with tempfile.TemporaryDirectory() as d:
        logger = AuditLogger(Path(d))
        logger.write_tombstone(
            TombstoneEntry(
                entry_type="skill",
                name="weather",
                action="disable",
                reason="unused",
                runbook="audit",
                origin="auto",
            )
        )
        results = logger.query_tombstones(domain="evolution")
        assert isinstance(results, list)
        results_no_filter = logger.query_tombstones()
        assert len(results_no_filter) == 1
