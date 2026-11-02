"""The group-state store: one row per chat, CAS writes and bounded snapshots."""

from __future__ import annotations

import json
import sqlite3
import stat

import pytest
from yeoman_gateway.group_state.contract import ContributorRegistry, GroupStateContributor
from yeoman_gateway.group_state.models import SCHEMA_VERSION, GroupState, Item, Section
from yeoman_gateway.group_state.store import GroupStateStore


def _section(
    key: str,
    *,
    revision: int = 1,
    items: tuple[Item, ...] = (),
    sources: tuple[str, ...] | None = None,
) -> Section:
    return Section(
        key=key,
        schema_version=1,
        trust="runtime" if key in {"presence", "budgets"} else "derived",
        layer="live",
        produced_at_ms=1_000,
        ttl_ms=600_000,
        source_cursor=f"m{revision}",
        period_start_ms=None,
        period_end_ms=None,
        sources=sources or (f"m{revision}",),
        data={"messages_30m": revision},
        items=items,
    )


def _state(channel: str = "whatsapp", chat_id: str = "group@g.us", revision: int = 1) -> GroupState:
    return GroupState(
        channel=channel,
        chat_id=chat_id,
        schema_version=SCHEMA_VERSION,
        revision=revision,
        history_cursor=f"m{revision}",
        updated_ms=1_000 + revision,
        sections={"presence": _section("presence", revision=revision)},
    )


def _item(item_id: str = "label-1", *, source: str = "m1", produced_at_ms: int = 1_000) -> Item:
    return Item(
        id=item_id,
        kind="label",
        data={"label": "synthetic"},
        sources=(source,),
        source_fingerprints={source: f"fp-{source}"},
        produced_at_ms=produced_at_ms,
        ttl_ms=600_000,
        trust="derived",
        conversation_id="conversation-1",
    )


def _write_v1_database(path) -> None:
    """A schema-v1 store with one current document and two snapshots."""
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE group_state (
                channel TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                schema_version INTEGER NOT NULL,
                updated_ms INTEGER NOT NULL,
                document TEXT NOT NULL,
                PRIMARY KEY (channel, chat_id)
            );
            CREATE TABLE group_state_snapshots (
                channel TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                updated_ms INTEGER NOT NULL,
                document TEXT NOT NULL,
                PRIMARY KEY (channel, chat_id, revision)
            );
            CREATE INDEX idx_group_state_snapshots_chat
                ON group_state_snapshots(channel, chat_id, revision DESC);
            """
        )
        connection.execute("INSERT INTO meta VALUES ('schema_version', '1')")
        for revision in (1, 2):
            document = GroupState(
                channel="whatsapp",
                chat_id="group@g.us",
                schema_version=1,
                revision=revision,
                history_cursor=f"m{revision}",
                updated_ms=1_000 + revision,
                sections={"presence": _section("presence", revision=revision)},
            ).to_json()
            connection.execute(
                "INSERT INTO group_state_snapshots VALUES (?, ?, ?, ?, ?)",
                ("whatsapp", "group@g.us", revision, 1_000 + revision, document),
            )
            if revision == 2:
                connection.execute(
                    "INSERT INTO group_state VALUES (?, ?, ?, 1, ?, ?)",
                    ("whatsapp", "group@g.us", revision, 1_000 + revision, document),
                )
        connection.commit()
    finally:
        connection.close()


def test_put_cas_insert_then_conflict(tmp_path) -> None:
    store = GroupStateStore(tmp_path / "group-state.db")
    try:
        assert store.put(_state(revision=1), expected_revision=None) is True
        # An insert with an expectation must not overwrite an existing row.
        assert store.put(_state(revision=2), expected_revision=None) is False
        assert store.get("whatsapp", "group@g.us") == _state(revision=1)

        assert store.put(_state(revision=2), expected_revision=1) is True
        assert store.put(_state(revision=3), expected_revision=1) is False
        assert store.get("whatsapp", "group@g.us") == _state(revision=2)
    finally:
        store.close()


def test_snapshots_pruned_to_kept(tmp_path) -> None:
    store = GroupStateStore(tmp_path / "group-state.db", snapshots_kept=2)
    try:
        assert store.put(_state(revision=1), expected_revision=None) is True
        for revision in range(2, 4):
            assert store.put(_state(revision=revision), expected_revision=revision - 1) is True
        snapshots = store.snapshots("whatsapp", "group@g.us", limit=10)
        assert [item.revision for item in snapshots] == [3, 2]
    finally:
        store.close()


def test_purge_chat_only_that_chat(tmp_path) -> None:
    store = GroupStateStore(tmp_path / "group-state.db")
    try:
        assert store.put(_state(chat_id="a@g.us", revision=1), expected_revision=None) is True
        assert store.put(_state(chat_id="b@g.us", revision=1), expected_revision=None) is True
        assert store.purge_chat("whatsapp", "a@g.us") == 2
        assert store.get("whatsapp", "a@g.us") is None
        assert store.snapshots("whatsapp", "a@g.us", limit=5) == []
        assert store.get("whatsapp", "b@g.us") == _state(chat_id="b@g.us", revision=1)
        assert store.list_chats() == [("whatsapp", "b@g.us", 1, 1_001)]
    finally:
        store.close()


def test_file_mode_0600(tmp_path) -> None:
    path = tmp_path / "group-state.db"
    store = GroupStateStore(path)
    try:
        store.put(_state(), expected_revision=None)
    finally:
        store.close()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_merge_keeps_other_writers_sections(tmp_path) -> None:
    store = GroupStateStore(tmp_path / "group-state.db")
    try:
        assert store.put(_state(revision=1), expected_revision=None) is True
        merged = store.merge_sections(
            "whatsapp",
            "group@g.us",
            {"budgets": _section("budgets", revision=2)},
            updated_ms=2_000,
        )
        assert merged is not None
        assert merged.revision == 2
        assert set(merged.sections) == {"presence", "budgets"}
        assert merged.sections["presence"] == _section("presence", revision=1)
        assert merged.sections["budgets"] == _section("budgets", revision=2)
        assert store.get("whatsapp", "group@g.us") == merged
        assert store.snapshots("whatsapp", "group@g.us", limit=5)[0] == merged
    finally:
        store.close()


def test_merge_after_purge_with_require_existing_returns_none(tmp_path) -> None:
    store = GroupStateStore(tmp_path / "group-state.db")
    try:
        assert (
            store.merge_sections(
                "whatsapp", "group@g.us", {"presence": _section("presence")}, updated_ms=1_000
            )
            is not None
        )
        store.purge_chat("whatsapp", "group@g.us")
        assert (
            store.merge_sections(
                "whatsapp",
                "group@g.us",
                {"budgets": _section("budgets")},
                updated_ms=2_000,
                require_existing=True,
            )
            is None
        )
        assert store.get("whatsapp", "group@g.us") is None
    finally:
        store.close()


def test_scrub_removes_items_from_current_and_snapshots(tmp_path) -> None:
    store = GroupStateStore(tmp_path / "group-state.db", snapshots_kept=5)
    try:
        base = _state(revision=1)
        item = _item()
        sections = {
            "presence": base.sections["presence"],
            "conversations": _section("conversations", items=(item,)),
        }
        first = store.merge_sections("whatsapp", "group@g.us", sections, updated_ms=1_000)
        assert first is not None
        second = store.merge_sections(
            "whatsapp",
            "group@g.us",
            {"conversations": _section("conversations", revision=2, items=(item,))},
            updated_ms=2_000,
        )
        assert second is not None

        assert store.scrub_items("whatsapp", "group@g.us", {"m1"}) == 1
        current = store.get("whatsapp", "group@g.us")
        assert current is not None
        assert current.revision == 3
        assert current.updated_ms == 2_000
        assert current.sections["conversations"].items == ()
        assert current.sections["presence"] == base.sections["presence"]
        snapshots = store.snapshots("whatsapp", "group@g.us", limit=5)
        assert snapshots
        assert all(snapshot.sections["conversations"].items == () for snapshot in snapshots)
        # Already scrubbed: nothing more to remove.
        assert store.scrub_items("whatsapp", "group@g.us", {"m1"}) == 0
    finally:
        store.close()


def test_scrub_leaves_runtime_sections(tmp_path) -> None:
    store = GroupStateStore(tmp_path / "group-state.db")
    try:
        runtime = _section("presence")
        derived = _section("conversations", items=(_item(source="m9"),))
        state = store.merge_sections(
            "whatsapp",
            "group@g.us",
            {"presence": runtime, "conversations": derived},
            updated_ms=1_000,
        )
        assert state is not None
        assert store.scrub_items("whatsapp", "group@g.us", {"m9"}) == 1
        current = store.get("whatsapp", "group@g.us")
        assert current is not None
        assert current.sections["presence"] == runtime
        assert current.sections["conversations"] == Section(
            key=derived.key,
            schema_version=derived.schema_version,
            trust=derived.trust,
            layer=derived.layer,
            produced_at_ms=derived.produced_at_ms,
            ttl_ms=derived.ttl_ms,
            source_cursor=derived.source_cursor,
            period_start_ms=derived.period_start_ms,
            period_end_ms=derived.period_end_ms,
            sources=derived.sources,
            data=derived.data,
        )
    finally:
        store.close()


def test_runs_purged_with_chat(tmp_path) -> None:
    store = GroupStateStore(tmp_path / "group-state.db")
    try:
        store.record_run(
            "whatsapp",
            "a@g.us",
            run_id="run-1",
            kind="label",
            started_ms=1_000,
            input_hash="hash",
            prompt_version="v1",
            model="synthetic",
            outcome="ok",
            prompt_tokens=3,
            completion_tokens=5,
        )
        store.record_run(
            "whatsapp",
            "b@g.us",
            run_id="run-2",
            kind="label",
            started_ms=1_000,
            input_hash="hash",
            prompt_version="v1",
            model="synthetic",
            outcome="ok",
        )
        store.purge_chat("whatsapp", "a@g.us")
        rows = store._conn.execute(
            "SELECT channel, chat_id, run_id FROM group_state_runs ORDER BY chat_id"
        ).fetchall()
        assert [(row[0], row[1], row[2]) for row in rows] == [("whatsapp", "b@g.us", "run-2")]
    finally:
        store.close()


def test_prune_runs(tmp_path) -> None:
    store = GroupStateStore(tmp_path / "group-state.db")
    try:
        for index, started_ms in enumerate((1_000, 2_000)):
            store.record_run(
                "whatsapp",
                "group@g.us",
                run_id=f"run-{index}",
                kind="label",
                started_ms=started_ms,
                input_hash="hash",
                prompt_version="v1",
                model="synthetic",
                outcome="ok",
            )
        assert store.prune_runs(older_than_ms=2_000) == 1
        rows = store._conn.execute("SELECT run_id FROM group_state_runs ORDER BY run_id").fetchall()
        assert [row[0] for row in rows] == ["run-1"]
    finally:
        store.close()


def test_migrate_v1_documents_and_snapshots(tmp_path) -> None:
    path = tmp_path / "group-state.db"
    _write_v1_database(path)
    store = GroupStateStore(path)
    try:
        state = store.get("whatsapp", "group@g.us")
        assert state is not None
        assert state.schema_version == 2
        assert state.revision == 2
        assert state.sections["presence"].items == ()
        snapshots = store.snapshots("whatsapp", "group@g.us", limit=5)
        assert [snapshot.revision for snapshot in snapshots] == [2, 1]
        assert all(snapshot.schema_version == 2 for snapshot in snapshots)
        assert all(snapshot.sections["presence"].items == () for snapshot in snapshots)
        assert (
            store._conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
            == "2"
        )
        assert (
            store._conn.execute(
                "SELECT schema_version, document FROM group_state WHERE chat_id = 'group@g.us'"
            ).fetchone()[0]
            == 2
        )
    finally:
        store.close()


def test_newer_schema_still_raises(tmp_path) -> None:
    path = tmp_path / "group-state.db"
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO meta VALUES ('schema_version', '3')")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(RuntimeError, match="newer"):
        GroupStateStore(path)


def test_migrate_is_idempotent(tmp_path) -> None:
    path = tmp_path / "group-state.db"
    _write_v1_database(path)
    store = GroupStateStore(path)
    try:
        before = store._conn.execute(
            "SELECT document FROM group_state_snapshots"
            " WHERE chat_id = 'group@g.us' ORDER BY revision"
        ).fetchall()
        before = [json.loads(row[0]) for row in before]
    finally:
        store.close()
    store = GroupStateStore(path)
    try:
        after = store._conn.execute(
            "SELECT document FROM group_state_snapshots"
            " WHERE chat_id = 'group@g.us' ORDER BY revision"
        ).fetchall()
        after = [json.loads(row[0]) for row in after]
        assert after == before
        assert all(document["schema_version"] == 2 for document in after)
    finally:
        store.close()


def test_cursor_round_trip(tmp_path) -> None:
    store = GroupStateStore(tmp_path / "group-state.db")
    try:
        assert store.invalidation_cursor("whatsapp", "group@g.us") == (0, "")
        store.set_invalidation_cursor("whatsapp", "group@g.us", 1_234, "event-2")
        assert store.invalidation_cursor("whatsapp", "group@g.us") == (1_234, "event-2")
        store.set_invalidation_cursor("whatsapp", "group@g.us", 1_500, "event-3")
        assert store.invalidation_cursor("whatsapp", "group@g.us") == (1_500, "event-3")
        assert store.invalidation_cursor("whatsapp", "other@g.us") == (0, "")
    finally:
        store.close()


class _Contributor:
    key = "presence"
    schema_version = 1
    trust = "runtime"
    layer = "live"

    def applies_to(self, channel: str, chat_kind: str) -> bool:
        return True

    async def build(self, ctx):  # pragma: no cover - registry only
        raise AssertionError("not built")

    def migrate(self, from_version: int, data):  # pragma: no cover - registry only
        return data


def test_registry_rejects_duplicate_key() -> None:
    registry = ContributorRegistry()
    first: GroupStateContributor = _Contributor()
    registry.register(first)
    with pytest.raises(ValueError, match="presence"):
        registry.register(_Contributor())
    assert registry.for_chat("whatsapp", "group") == (first,)
