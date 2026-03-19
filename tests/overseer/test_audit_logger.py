"""Tests for JSONL audit logger and tombstones."""
from __future__ import annotations
import json
from pathlib import Path
from yeoman_overseer.audit.logger import AuditLogger, AuditEntry, TombstoneEntry

def test_append_and_read(tmp_path: Path) -> None:
    logger = AuditLogger(tmp_path / "audit")
    entry = AuditEntry(runbook="gateway-health", trigger="poll", action="restart_service", target="yeoman-gateway", result="success", duration_ms=3200, escalated_to_llm=False)
    logger.append(entry)
    entries = logger.read_recent(limit=10)
    assert len(entries) == 1
    assert entries[0]["runbook"] == "gateway-health"

def test_read_recent_limit(tmp_path: Path) -> None:
    logger = AuditLogger(tmp_path / "audit")
    for i in range(20):
        logger.append(AuditEntry(runbook=f"test-{i}", trigger="cron", action="noop", target="x", result="success", duration_ms=0, escalated_to_llm=False))
    entries = logger.read_recent(limit=5)
    assert len(entries) == 5
    assert entries[0]["runbook"] == "test-19"

def test_read_by_domain(tmp_path: Path) -> None:
    logger = AuditLogger(tmp_path / "audit")
    logger.append(AuditEntry(runbook="health-gw", trigger="poll", action="restart", target="gw", result="success", duration_ms=0, escalated_to_llm=False, domain="health"))
    logger.append(AuditEntry(runbook="ops-log", trigger="cron", action="rotate", target="logs", result="success", duration_ms=0, escalated_to_llm=False, domain="ops"))
    entries = logger.read_recent(limit=10, domain="health")
    assert len(entries) == 1
    assert entries[0]["runbook"] == "health-gw"

def test_tombstone_write_and_query(tmp_path: Path) -> None:
    logger = AuditLogger(tmp_path / "audit")
    tomb = TombstoneEntry(entry_type="skill", name="weather", action="disabled", reason="unused 28 days", runbook="skill-audit", origin="auto")
    logger.write_tombstone(tomb)
    tombstones = logger.query_tombstones(name="weather")
    assert len(tombstones) == 1
    assert tombstones[0]["name"] == "weather"

def test_tombstone_query_no_match(tmp_path: Path) -> None:
    logger = AuditLogger(tmp_path / "audit")
    assert logger.query_tombstones(name="nonexistent") == []
