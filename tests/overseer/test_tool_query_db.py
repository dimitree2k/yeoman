import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

from yeoman_overseer.agent.tools.query_db import execute


def _ctx():
    return MagicMock()


def _make_db(tmp_path: Path) -> Path:
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE items (id INTEGER, name TEXT)")
    conn.execute("INSERT INTO items VALUES (1, 'alpha')")
    conn.execute("INSERT INTO items VALUES (2, 'beta')")
    conn.commit()
    conn.close()
    return db


def test_select_returns_rows(tmp_path):
    db = _make_db(tmp_path)
    result = execute({"db_path": str(db), "query": "SELECT * FROM items"}, _ctx())
    assert "alpha" in result
    assert "beta" in result


def test_select_with_filter(tmp_path):
    db = _make_db(tmp_path)
    result = execute(
        {"db_path": str(db), "query": "SELECT name FROM items WHERE id = 1"}, _ctx()
    )
    assert "alpha" in result


def test_write_attempt_is_rejected(tmp_path):
    db = _make_db(tmp_path)
    result = execute(
        {"db_path": str(db), "query": "INSERT INTO items VALUES (3, 'gamma')"}, _ctx()
    )
    assert "error" in result.lower() or "readonly" in result.lower()


def test_drop_attempt_is_rejected(tmp_path):
    db = _make_db(tmp_path)
    result = execute({"db_path": str(db), "query": "DROP TABLE items"}, _ctx())
    assert "error" in result.lower() or "readonly" in result.lower()


def test_missing_db_returns_error(tmp_path):
    result = execute(
        {"db_path": str(tmp_path / "nope.db"), "query": "SELECT 1"}, _ctx()
    )
    assert "error" in result.lower()
