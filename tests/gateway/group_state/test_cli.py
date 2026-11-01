"""CLI coverage for ``yeoman group-state show|diff|purge``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner
from yeoman_gateway.cli.commands import app
from yeoman_gateway.group_state.models import GroupState, Section
from yeoman_gateway.group_state.store import GroupStateStore

CHAT = "group@g.us"


def _section(key: str, *, messages: int, at_ms: int = 1_000) -> Section:
    return Section(
        key=key,
        schema_version=1,
        trust="runtime",
        layer="live",
        produced_at_ms=at_ms,
        ttl_ms=600_000,
        source_cursor=f"m{messages}",
        period_start_ms=None,
        period_end_ms=None,
        sources=(f"m{messages}",),
        data={"messages": messages},
    )


def _state(
    revision: int, *, messages: int = 1, sections: dict[str, Section] | None = None
) -> GroupState:
    chosen = {"presence": _section("presence", messages=messages)} if sections is None else sections
    return GroupState(
        channel="whatsapp",
        chat_id=CHAT,
        schema_version=1,
        revision=revision,
        history_cursor=f"m{messages}",
        updated_ms=1_000 + revision,
        sections=chosen,
    )


def _store(path: Path) -> GroupStateStore:
    return GroupStateStore(path)


def _invoke(*args: str):
    return CliRunner().invoke(app, ["group-state", *args])


def _seed(tmp_path: Path) -> tuple[Path, GroupState]:
    path = tmp_path / "group-state.db"
    store = _store(path)
    try:
        state = _state(3, messages=7)
        assert store.put(state, expected_revision=None)
    finally:
        store.close()
    return path, state


def test_show_prints_sections(tmp_path) -> None:
    path, state = _seed(tmp_path)
    result = _invoke("show", "--chat", CHAT, "--db-path", str(path))
    assert result.exit_code == 0, result.output
    assert "revision: 3" in result.output
    assert "history cursor: m7" in result.output
    assert "presence" in result.output
    assert "runtime" in result.output
    assert '"messages": 7' in result.output


def test_show_json(tmp_path) -> None:
    path, state = _seed(tmp_path)
    result = _invoke("show", "--chat", CHAT, "--db-path", str(path), "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["chat_id"] == CHAT
    assert payload["revision"] == 3
    assert payload["sections"]["presence"]["data"] == {"messages": 7}


def test_show_no_activity(tmp_path) -> None:
    path = tmp_path / "group-state.db"
    store = _store(path)
    try:
        empty = _state(1, sections={})
        assert store.put(empty, expected_revision=None)
    finally:
        store.close()
    result = _invoke("show", "--chat", CHAT, "--db-path", str(path))
    assert result.exit_code == 0, result.output
    assert "no activity" in result.output


def test_show_unknown_chat_exit_1(tmp_path) -> None:
    path, _ = _seed(tmp_path)
    result = _invoke("show", "--chat", "missing@g.us", "--db-path", str(path))
    assert result.exit_code == 1
    assert "No group state" in result.output


def test_diff_two_revisions(tmp_path) -> None:
    path = tmp_path / "group-state.db"
    store = _store(path)
    try:
        assert store.put(_state(1, messages=1), expected_revision=None)
        changed = {
            "presence": _section("presence", messages=2),
            "budgets": _section("budgets", messages=2),
        }
        assert store.put(_state(2, messages=2, sections=changed), expected_revision=1)
    finally:
        store.close()
    result = _invoke("diff", "--chat", CHAT, "--db-path", str(path), "--revisions", "2")
    assert result.exit_code == 0, result.output
    assert "revision 1 -> 2" in result.output
    assert "~ presence" in result.output
    assert "+ budgets" in result.output


def test_purge_requires_yes(tmp_path) -> None:
    path, _ = _seed(tmp_path)
    refused = _invoke("purge", "--chat", CHAT, "--db-path", str(path))
    assert refused.exit_code == 1
    store = _store(path)
    try:
        assert store.get("whatsapp", CHAT) is not None
    finally:
        store.close()
    accepted = _invoke("purge", "--chat", CHAT, "--db-path", str(path), "--yes")
    assert accepted.exit_code == 0, accepted.output
    assert "purged 2" in accepted.output
    store = _store(path)
    try:
        assert store.get("whatsapp", CHAT) is None
        assert store.snapshots("whatsapp", CHAT, limit=5) == []
    finally:
        store.close()


@pytest.mark.parametrize("subject", ["Exact Subject"])
def test_show_resolves_exact_subject(tmp_path, subject: str) -> None:
    import sqlite3

    path, _ = _seed(tmp_path)
    history = tmp_path / "history.db"
    with sqlite3.connect(history) as conn:
        conn.execute(
            "CREATE TABLE message_events (event_id TEXT, kind TEXT, channel TEXT, chat_id TEXT,"
            " occurred_ms INTEGER, payload_json TEXT)"
        )
        conn.execute(
            "INSERT INTO message_events VALUES ('e1','group_subject','whatsapp',?,1,?)",
            (CHAT, json.dumps({"value": subject, "snapshot": True})),
        )
    result = _invoke(
        "show", "--chat", subject, "--db-path", str(path), "--history-db", str(history)
    )
    assert result.exit_code == 0, result.output
    assert "revision: 3" in result.output
