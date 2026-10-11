from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from yeoman_overseer.agent.tools import prune_memory


@pytest.mark.parametrize("name", [None, "memory.db", "history.db", "knowledge.db"])
def test_prune_fails_closed_without_opening_retired_store(tmp_path, monkeypatch, name):
    db_path = tmp_path / "data" / name if name else None
    if db_path:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db_path.touch()
    ctx = SimpleNamespace(yeoman_home=tmp_path, memory_db=db_path)
    connect = Mock(side_effect=AssertionError("database opened"))
    copy = Mock(side_effect=AssertionError("snapshot created"))
    monkeypatch.setattr(prune_memory.sqlite3, "connect", connect)
    monkeypatch.setattr(prune_memory.shutil, "copy2", copy)

    result = prune_memory.prune_memory(age_days=30, ctx=ctx)

    assert result["ok"] is False
    connect.assert_not_called()
    copy.assert_not_called()
