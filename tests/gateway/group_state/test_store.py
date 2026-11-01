"""The group-state store: one row per chat, CAS writes and bounded snapshots."""

from __future__ import annotations

import stat

import pytest
from yeoman_gateway.group_state.contract import ContributorRegistry, GroupStateContributor
from yeoman_gateway.group_state.models import GroupState, Section
from yeoman_gateway.group_state.store import GroupStateStore


def _state(channel: str = "whatsapp", chat_id: str = "group@g.us", revision: int = 1) -> GroupState:
    section = Section(
        key="presence",
        schema_version=1,
        trust="runtime",
        layer="live",
        produced_at_ms=1_000,
        ttl_ms=600_000,
        source_cursor=f"m{revision}",
        period_start_ms=None,
        period_end_ms=None,
        sources=(f"m{revision}",),
        data={"messages_30m": revision},
    )
    return GroupState(
        channel=channel,
        chat_id=chat_id,
        schema_version=1,
        revision=revision,
        history_cursor=f"m{revision}",
        updated_ms=1_000 + revision,
        sections={"presence": section},
    )


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
