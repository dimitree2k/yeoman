"""Tests for JSONL audit logger and tombstones."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import yeoman_overseer.audit.logger as audit_module
from yeoman_overseer.audit.logger import AuditEntry, AuditLogger, TombstoneEntry


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


def test_suppresses_only_noop_cron_success(tmp_path: Path) -> None:
    logger = AuditLogger(tmp_path / "audit")
    noop = AuditEntry("digest", "cron", "triggered", "", "success", 0, False)
    assert logger.append(noop) is None
    assert logger.read_recent() == []

    meaningful = [
        AuditEntry("digest", "cron", "send_message", "owner", "success", 1, False),
        AuditEntry("digest", "cron", "triggered", "", "error: down", 1, False),
        AuditEntry("digest", "cron", "triggered", "", "success", 1, True),
        AuditEntry("health", "poll", "triggered", "gateway", "success", 1, False),
    ]
    for entry in meaningful:
        assert logger.append(entry) is not None
    assert [row["action"] for row in logger.read_recent(limit=10)] == [
        "triggered", "triggered", "triggered", "send_message",
    ]


def test_monthly_logs_read_across_utc_month_boundary(tmp_path: Path, monkeypatch) -> None:
    logger = AuditLogger(tmp_path / "audit")

    class Clock:
        current = datetime(2026, 3, 31, 23, 59, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls.current

    monkeypatch.setattr(audit_module, "datetime", Clock)
    logger.append(AuditEntry("march", "cron", "rotate", "", "changed", 1, False))
    legacy = {
        "runbook": "legacy-day", "domain": "ops", "ts": "2026-03-31T23:58:00+00:00",
    }
    (tmp_path / "audit" / "2026-03-31.jsonl").write_text(json.dumps(legacy) + "\n")
    Clock.current = datetime(2026, 4, 1, 0, 1, tzinfo=timezone.utc)
    logger.append(AuditEntry("april", "cron", "rotate", "", "changed", 1, False))

    assert sorted(path.name for path in (tmp_path / "audit").glob("????-??.jsonl")) == [
        "2026-03.jsonl", "2026-04.jsonl",
    ]
    assert (tmp_path / "audit" / "2026-03-31.jsonl").read_text() == json.dumps(legacy) + "\n"
    assert [row["runbook"] for row in logger.read_recent(limit=3)] == [
        "april", "march", "legacy-day",
    ]
    assert json.loads((tmp_path / "audit" / "2026-03.jsonl").read_text())["ts"].startswith("2026-03-31T23:59")


def test_monthly_logs_keep_tombstones_independent(tmp_path: Path) -> None:
    logger = AuditLogger(tmp_path / "audit")
    logger.write_tombstone(TombstoneEntry("skill", "retired", "disabled", "done", "audit"))
    assert logger.query_tombstones(name="retired")[0]["name"] == "retired"
