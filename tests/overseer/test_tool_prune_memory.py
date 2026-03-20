# tests/overseer/test_tool_prune_memory.py
from __future__ import annotations
import sqlite3
import shutil
from pathlib import Path
from unittest.mock import MagicMock
import pytest
from yeoman_overseer.agent.tools.prune_memory import prune_memory


def _ctx(tmp_path: Path, db_path: Path) -> MagicMock:
    ctx = MagicMock()
    ctx.memory_db = db_path
    ctx.runbook_name = "memory-prune"
    ctx.domain = "memory"
    ctx.audit = MagicMock()
    return ctx


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE memory2_nodes "
        "(id INTEGER PRIMARY KEY, content TEXT, salience REAL, created_at REAL, domain TEXT)"
    )
    import time
    now = time.time()
    conn.execute("INSERT INTO memory2_nodes VALUES (1, 'old low', 0.1, ?, 'general')", (now - 40 * 86400,))
    conn.execute("INSERT INTO memory2_nodes VALUES (2, 'recent high', 0.9, ?, 'general')", (now,))
    conn.commit()
    conn.close()


def test_snapshot_created_before_deletion(tmp_path):
    db = tmp_path / "memory.db"
    _make_db(db)
    ctx = _ctx(tmp_path, db)
    result = prune_memory(age_days=30, salience_below=0.5, ctx=ctx)
    assert result["ok"] is True
    snapshots = list(tmp_path.glob("memory.db.snapshot-*"))
    assert len(snapshots) == 1


def test_deletes_old_low_salience_rows(tmp_path):
    db = tmp_path / "memory.db"
    _make_db(db)
    ctx = _ctx(tmp_path, db)
    result = prune_memory(age_days=30, salience_below=0.5, ctx=ctx)
    assert result["rows_deleted"] == 1
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT id FROM memory2_nodes").fetchall()
    conn.close()
    assert rows == [(2,)]


def test_domain_filter(tmp_path):
    db = tmp_path / "memory.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE memory2_nodes "
        "(id INTEGER PRIMARY KEY, content TEXT, salience REAL, created_at REAL, domain TEXT)"
    )
    import time
    now = time.time()
    conn.execute("INSERT INTO memory2_nodes VALUES (1, 'old', 0.1, ?, 'health')", (now - 40 * 86400,))
    conn.execute("INSERT INTO memory2_nodes VALUES (2, 'old', 0.1, ?, 'memory')", (now - 40 * 86400,))
    conn.commit()
    conn.close()
    ctx = _ctx(tmp_path, db)
    result = prune_memory(age_days=30, salience_below=0.5, domain="health", ctx=ctx)
    assert result["rows_deleted"] == 1
    conn = sqlite3.connect(db)
    remaining = conn.execute("SELECT id FROM memory2_nodes").fetchall()
    conn.close()
    assert (2,) in remaining


def test_audit_logged(tmp_path):
    db = tmp_path / "memory.db"
    _make_db(db)
    ctx = _ctx(tmp_path, db)
    prune_memory(age_days=30, salience_below=0.5, ctx=ctx)
    ctx.audit.append.assert_called_once()
    entry = ctx.audit.append.call_args[0][0]
    assert entry.action == "prune_memory"
