"""Retired pruning never snapshots, opens or deletes legacy memory."""
from types import SimpleNamespace
from unittest.mock import Mock

from yeoman_overseer.agent.tools.prune_memory import prune_memory


def test_prune_refuses_without_touching_storage(tmp_path, monkeypatch):
    import shutil
    import sqlite3

    path = tmp_path / "memory.db"
    path.write_bytes(b"preserved")
    copied = Mock(side_effect=AssertionError("snapshot created"))
    opened = Mock(side_effect=AssertionError("database opened"))
    monkeypatch.setattr(shutil, "copy2", copied)
    monkeypatch.setattr(sqlite3, "connect", opened)
    result = prune_memory(age_days=30, salience_below=0.5, domain="memory",
                          ctx=SimpleNamespace(memory_db=path))
    assert result == {"ok": False, "error": "legacy history prune refused; use Knowledge maintenance"}
    copied.assert_not_called()
    opened.assert_not_called()
    assert path.read_bytes() == b"preserved"
