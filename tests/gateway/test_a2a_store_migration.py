from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from yeoman_gateway.a2a import relay
from yeoman_gateway.a2a.store_migration import migrate_a2a_stores
from yeoman_gateway.agent.tools.a2a_research import (
    A2AResearchStore,
    PendingResearch,
    a2a_store_path,
)
from yeoman_shared.utils.helpers import get_operational_store_path


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_migration_copies_all_tables_keys_and_relationships_read_only(tmp_path: Path) -> None:
    relay_path = tmp_path / "legacy-relay.db"
    research_path = tmp_path / "legacy-research.db"
    target = tmp_path / "ops" / "a2a.db"
    relay._RelayStore(relay_path)
    research = A2AResearchStore(research_path)
    invocation = {"skill": "research.deep", "input": {"idempotency_key": "key-1"}}
    with relay._RelayStore(relay_path)._connect() as connection:
        connection.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?)",
            ("task-1", "peer-1", "ctx-1", '["prior"]', '{"id":"task-1"}'),
        )
        connection.execute(
            "INSERT INTO idempotency(peer, skill, idempotency_key, request_hash, task_id, effect_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("peer-1", "research.deep", "key-1", relay.canonical_request_hash(invocation), "task-1", "effect-1"),
        )
        connection.execute(
            "INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("opaque-1", "peer-1", "effect-1", "/private/artifact", "image/png", 0, "sha", 7, 9999999999),
        )
    research.put(
        PendingResearch(
            task_id="research-1", worker="worker", skill="research.deep", context_id="ctx-r",
            reference_task_ids=("prior-r",), channel="whatsapp", chat_id="chat", effect_id="effect-r",
            question="question", created_ms=123, canonical_user_id="user", symbol="XYZ", length="long", thread_id="thread",
        )
    )
    research.save_report("effect-r", channel="whatsapp", chat_id="chat", content="report", canonical_user_id="user", symbol="XYZ", card="card", signal="HOLD")
    source_digests = (_digest(relay_path), _digest(research_path))

    report = migrate_a2a_stores(relay_path, research_path, target)

    assert report.table_counts == {
        "artifacts": 1,
        "completed_reports": 1,
        "idempotency": 1,
        "pending_research": 1,
        "tasks": 1,
    }
    assert report.primary_keys == {
        "artifacts": ["opaque-1"],
        "completed_reports": ["effect-r"],
        "idempotency": [["peer-1", "research.deep", "key-1"]],
        "pending_research": ["research-1"],
        "tasks": ["task-1"],
    }
    with sqlite3.connect(target) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert "tasks_peer_id" in {
            row[1] for row in connection.execute("PRAGMA index_list(tasks)")
        }
        assert [tuple(row[2:5]) for row in connection.execute("PRAGMA foreign_key_list(idempotency)")] == [
            ("tasks", "task_id", "task_id")
        ]
        assert "thread_id" in {
            row[1] for row in connection.execute("PRAGMA table_info(pending_research)")
        }
        assert connection.execute("SELECT task_id FROM idempotency").fetchone() == ("task-1",)
        assert connection.execute("SELECT reference_task_ids FROM tasks").fetchone() == ('["prior"]',)
        assert connection.execute("SELECT content FROM completed_reports").fetchone() == ("report",)
        assert connection.execute("SELECT reference_task_ids FROM pending_research").fetchone() == ('[\"prior-r\"]',)
    assert (_digest(relay_path), _digest(research_path)) == source_digests
    assert not relay_path.with_name(relay_path.name + "-wal").exists()
    assert not research_path.with_name(research_path.name + "-wal").exists()


def test_migration_refuses_existing_target_without_changing_it(tmp_path: Path) -> None:
    relay_path = tmp_path / "relay.db"
    research_path = tmp_path / "research.db"
    target = tmp_path / "target.db"
    relay._RelayStore(relay_path)
    A2AResearchStore(research_path)
    target.write_bytes(b"existing target")
    before = _digest(target)

    with pytest.raises(ValueError, match="target already exists"):
        migrate_a2a_stores(relay_path, research_path, target)

    assert _digest(target) == before
    assert _digest(relay_path)
    assert _digest(research_path)


def test_migration_refuses_source_missing_required_schema(tmp_path: Path) -> None:
    relay_path = tmp_path / "relay.db"
    research_path = tmp_path / "research.db"
    target = tmp_path / "target.db"
    relay._RelayStore(relay_path)
    with sqlite3.connect(research_path):
        pass

    with pytest.raises(ValueError, match="missing required A2A tables"):
        migrate_a2a_stores(relay_path, research_path, target)

    assert not target.exists()


def test_fresh_start_creates_only_canonical_a2a_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path))
    monkeypatch.setenv("YEOMAN_A2A_BIND_HOST", "127.0.0.1")
    monkeypatch.setenv("YEOMAN_A2A_ALLOWED_PEER_IPS", "127.0.0.1")
    monkeypatch.setenv("YEOMAN_A2A_PEER_ID", "test-peer")
    monkeypatch.setenv("YEOMAN_A2A_BEARER_SECRET", "test-secret")
    monkeypatch.setenv("YEOMAN_A2A_PUBLIC_URL", "https://example.test")
    monkeypatch.setenv("YEOMAN_A2A_CONTENT_TYPES", "text")
    monkeypatch.setenv("YEOMAN_A2A_WHATSAPP_ENABLED", "false")
    for name in ("YEOMAN_A2A_STATE_PATH", "YEOMAN_A2A_SOCKET_PATH", "YEOMAN_A2A_SOCKET"):
        monkeypatch.delenv(name, raising=False)

    config = relay.RelayConfig.from_env()
    relay._RelayStore(config.state_path)
    research_path = a2a_store_path(
        SimpleNamespace(path=get_operational_store_path("processing", data_dir=tmp_path / "data"))
    )
    assert research_path == get_operational_store_path("a2a")
    A2AResearchStore(research_path)

    assert config.state_path == get_operational_store_path("a2a", data_dir=tmp_path / "data")
    assert config.state_path.is_file()
    assert not (tmp_path / "data" / "a2a" / "relay.db").exists()
    assert not (tmp_path / "data" / "processing" / "a2a-research.db").exists()
