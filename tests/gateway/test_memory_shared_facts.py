"""Plan 05 / Aufgabe 1: shared facts live beside legacy memory, never as a rewrite of it."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from yeoman_gateway.memory.models import MemoryEntry
from yeoman_gateway.memory.shared_facts import (
    FactReadContext,
    FactSource,
    SharedFact,
    SharedFactStore,
    effective_audience,
)
from yeoman_gateway.memory.store import MemoryStore

WORKSPACE = "ws1"
CHAT = "gruppe-a"
T0 = 1_700_000_000_000


def _legacy_node(store: MemoryStore, content: str, *, node_id: str = "legacy-1") -> str:
    entry = MemoryEntry(
        id=node_id,
        workspace_id=WORKSPACE,
        scope_type="chat",
        scope_key=CHAT,
        sector="episodic",
        kind="note",
        content=content,
        content_norm=content.lower(),
        content_hash=f"hash-{node_id}",
        salience=0.5,
        confidence=0.5,
        source="legacy",
    )
    store.upsert_node(entry)
    return node_id


def _source(event_id: str = "ev1", revision: int = 1) -> FactSource:
    return FactSource(
        source_event_id=event_id,
        source_revision=revision,
        source_trace_id="tr1",
        author_principal="member-old",
        source_channel="whatsapp",
        source_chat_id=CHAT,
        occurred_ms=T0,
    )


def _fact(*, fact_id: str = "f1", audience=("member-old",), allowed=(), visibility="chat_shared"):
    return SharedFact(
        fact_id=fact_id,
        workspace_id=WORKSPACE,
        chat_scope_key=CHAT,
        content="Der Stammtisch ist donnerstags.",
        author_principal="member-old",
        assertion_status="assertion",
        visibility_scope=visibility,
        group_rule="chat_members_at_source",
        audience_snapshot_id="snap1",
        valid_from_ms=T0,
        extractor_version="v1",
        sources=(_source(),),
        allowed_principals=frozenset(allowed),
        audience=frozenset(audience),
    )


def test_legacy_node_never_becomes_a_shared_fact(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    _legacy_node(store, "Beispielinhalt ohne Faktzeile")

    facts = SharedFactStore(store)

    assert facts.list_facts() == []
    assert store.get_meta("memory_schema_version") == "2"
    assert store.get_meta("acl_epoch") == "1"
    store.close()


def test_migration_is_idempotent_and_keeps_the_file_healthy(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    store = MemoryStore(path)
    _legacy_node(store, "Bestandszeile")
    store.close()

    reopened = MemoryStore(path)
    reopened.close()
    third = MemoryStore(path)
    assert third.get_meta("memory_schema_version") == "2"
    assert SharedFactStore(third).list_facts() == []
    check = third._conn.execute("PRAGMA quick_check").fetchone()[0]
    assert check == "ok"
    rows = third._conn.execute(
        "SELECT COUNT(*) FROM memory2_nodes WHERE id = 'legacy-1'"
    ).fetchone()[0]
    assert rows == 1
    third.close()


def test_fact_round_trip_keeps_content_in_nodes(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    facts = SharedFactStore(store)

    facts.upsert_fact(_fact())

    stored = facts.get_fact("f1")
    assert stored is not None
    assert stored.content == "Der Stammtisch ist donnerstags."
    assert stored.assertion_status == "assertion"
    assert stored.sources == (_source(),)
    assert stored.audience == frozenset({"member-old"})
    assert [fact.fact_id for fact in facts.list_facts()] == ["f1"]
    node = store._conn.execute(
        "SELECT content FROM memory2_nodes WHERE id = 'f1'"
    ).fetchone()
    assert node["content"] == "Der Stammtisch ist donnerstags."
    store.close()


def test_missing_audience_rows_mean_nobody_not_everybody(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    facts = SharedFactStore(store)
    facts.upsert_fact(_fact(audience=(), allowed=()))

    stored = facts.get_fact("f1")

    assert stored is not None
    assert stored.audience == frozenset()
    store.close()


def test_allowed_principals_are_not_audience(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    facts = SharedFactStore(store)
    facts.upsert_fact(_fact(audience=("member-old",), allowed=("owner-1",)))

    stored = facts.get_fact("f1")

    assert stored is not None
    assert stored.audience == frozenset({"member-old"})
    assert stored.allowed_principals == frozenset({"owner-1"})
    store.close()


def test_effective_audience_intersects_and_never_unions() -> None:
    sources = (
        (frozenset({"a", "b", "c"}), "snap-1"),
        (frozenset({"b", "c", "d"}), "snap-2"),
    )

    assert effective_audience(
        source_audiences=sources,
        group_rule="chat_members_at_source",
        allowed_principals=frozenset(),
        audience_snapshots={},
    ) == frozenset({"b", "c"})


def test_effective_audience_empty_intersection_publishes_nothing() -> None:
    sources = ((frozenset({"a"}), "snap-1"), (frozenset({"b"}), "snap-2"))

    assert (
        effective_audience(
            source_audiences=sources,
            group_rule="chat_members_at_source",
            allowed_principals=frozenset(),
            audience_snapshots={},
        )
        == frozenset()
    )


def test_effective_audience_author_only_has_no_audience() -> None:
    assert (
        effective_audience(
            source_audiences=((frozenset({"a"}), "snap-1"),),
            group_rule="author_only",
            allowed_principals=frozenset({"owner"}),
            audience_snapshots={},
        )
        == frozenset()
    )


def test_effective_audience_unknown_snapshot_is_empty() -> None:
    assert (
        effective_audience(
            source_audiences=((None, "missing-snapshot"),),
            group_rule="chat_members_at_source",
            allowed_principals=frozenset(),
            audience_snapshots={},
        )
        == frozenset()
    )


def test_fact_tables_are_additive_only(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    store = MemoryStore(path)
    _legacy_node(store, "Bestandszeile")
    columns = {
        str(row["name"])
        for row in store._conn.execute("PRAGMA table_info(memory2_nodes)").fetchall()
    }
    store.close()

    assert "visibility_scope" not in columns
    assert "author_principal" not in columns
    conn = sqlite3.connect(path)
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    conn.close()
    assert {
        "memory2_facts",
        "memory2_fact_sources",
        "memory2_fact_principals",
        "memory2_fact_jobs",
    } <= tables


def test_read_context_membership_known_is_derived() -> None:
    known = FactReadContext(
        principal_id="member-old",
        chat_scope_key=CHAT,
        current_members=frozenset({"member-old"}),
        audience_snapshot_id="snap1",
        epoch=1,
        now_ms=T0,
        owner=False,
    )
    unknown = FactReadContext(
        principal_id="member-old",
        chat_scope_key=CHAT,
        current_members=None,
        audience_snapshot_id="snap1",
        epoch=1,
        now_ms=T0,
        owner=False,
    )

    assert known.membership_known is True
    assert unknown.membership_known is False
