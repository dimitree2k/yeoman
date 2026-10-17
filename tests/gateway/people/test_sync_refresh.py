"""Audit-only sync writes keep the immutable index; content changes invalidate it."""

import json
import threading

import pytest
from yeoman_gateway.people.resolver import PeopleResolver, resolve_person
from yeoman_gateway.people.service import PeopleSyncService


@pytest.fixture
def subject(directory_case):
    c = directory_case
    c.subject = c.add_person("Original Person", ids=(("phone", "4915000000011"),))
    c.add_name(c.subject, "Observed Alias")
    return c


def test_noop_sync_keeps_index_and_memo_and_reads_only_new_audit_rows(subject):
    c = subject
    reader = c.reader()
    assert resolve_person("Original Person", reader).person_id == c.subject
    index = c.directory._index()
    memo = c.directory._local.state.memo
    memo["witness"] = object()
    c.writer.execute("BEGIN")
    c.writer.execute("UPDATE persons SET last_seen_ms=500")
    c.writer.execute("UPDATE person_ids SET first_seen_ms=1,last_seen_ms=500")
    c.writer.execute("UPDATE person_names SET first_seen_ms=1,last_seen_ms=500")
    c.writer.execute("INSERT OR REPLACE INTO meta VALUES ('last_sync_ms','500')")
    c.writer.execute("COMMIT")
    sql = []
    c.directory._local.state.connection.set_trace_callback(sql.append)
    assert c.directory._index() is index
    assert c.directory._local.state.memo is memo and "witness" in memo
    assert resolve_person("Original Person", reader).person_id == c.subject
    assert not any(
        "FROM persons_v" in query or "FROM person_names" in query or "FROM person_ids" in query
        for query in sql
    )
    assert any("FROM change_log WHERE id >" in query for query in sql)


@pytest.mark.parametrize("edit", ["hidden", "merge", "new_name", "level"])
def test_content_edits_are_visible_on_next_call(subject, edit):
    c = subject
    reader = c.reader()
    old = c.directory._index()
    if edit == "hidden":
        c.writer.execute("UPDATE person_names SET hidden=1 WHERE name='Observed Alias'")
        assert resolve_person("Observed Alias", reader).status == "not_found"
    elif edit == "merge":
        c.writer.execute("UPDATE persons SET merged_into=? WHERE id=?", (c.counterpart, c.subject))
        assert resolve_person("Original Person", reader).person_id == c.counterpart
    elif edit == "new_name":
        c.add_name(c.subject, "Fresh Observed")
        assert resolve_person("Fresh Observed", reader).person_id == c.subject
    else:
        quiet = c.add_person("Quiet Uncurated", source="observed", level="seen")
        c.directory._index()
        assert resolve_person("Qui Unc", reader).status == "not_found"
        c.writer.execute("UPDATE persons SET level='contact' WHERE id=?", (quiet,))
        assert resolve_person("Qui Unc", reader).person_id == quiet
    assert c.directory._index() is not old


@pytest.mark.parametrize(
    "edit", ["schema", "missing_trigger", "rewind", "malformed", "unknown", "nonfinite"]
)
def test_unsafe_audit_state_forces_full_load(subject, edit):
    c = subject
    old = c.directory._index()
    if edit == "schema":
        c.writer.execute("CREATE TABLE synthetic_extra(value)")
    elif edit == "missing_trigger":
        c.writer.execute("DROP TRIGGER person_names_audit_update")
    elif edit == "rewind":
        c.writer.execute("DELETE FROM change_log")
    else:
        c.writer.execute(
            "INSERT INTO change_log(writer,tbl,row_id,op,old,new) VALUES ('sync',?,1,'update',?,?)",
            (
                "persons" if edit in ("malformed", "nonfinite") else "unknown_table",
                '{"last_seen_ms":NaN}' if edit == "nonfinite" else "{",
                json.dumps({}),
            ),
        )
    assert c.directory._index() is not old
    if edit == "missing_trigger":
        current = c.directory._index()
        c.writer.execute(
            "UPDATE person_names SET name='Unlogged Alias' WHERE name='Observed Alias'"
        )
        assert c.directory._index() is not current
        assert resolve_person("Unlogged Alias", c.reader()).person_id == c.subject


def test_known_non_index_table_changes_keep_index(subject):
    c = subject
    old = c.directory._index()
    c.writer.execute(
        "INSERT INTO suggestions(kind,person_a,person_b,evidence) VALUES ('merge',?,?, '{}')",
        (c.subject, c.counterpart),
    )
    assert c.directory._index() is old


def test_commit_racing_shortcut_is_not_skipped(subject):
    c = subject
    old = c.directory._index()
    c.writer.execute("INSERT OR REPLACE INTO meta VALUES ('last_sync_ms','500')")
    commits = []

    def racing_commit(sql):
        if sql == "ROLLBACK" and not commits:
            commits.append(True)
            c.writer.execute(
                "UPDATE persons SET preferred_name='Racing Edit' WHERE id=?", (c.subject,)
            )

    connection = c.directory._local.state.connection
    connection.set_trace_callback(racing_commit)
    assert c.directory._index() is old
    connection.set_trace_callback(None)
    assert commits
    assert resolve_person("Racing Edit", c.reader()).person_id == c.subject
    assert c.directory._index() is not old


def test_refresh_unavailable_is_safe(tmp_path):
    directory = PeopleResolver(tmp_path / "absent.db")
    assert directory.refresh() is None
    assert not directory.db_path.exists()


async def test_sync_refreshes_committed_index_on_event_loop_thread(tmp_path, monkeypatch):
    from tests.gateway.people.people_fixtures import level_history

    history = level_history(tmp_path / "history")
    directory = PeopleResolver(tmp_path / "people.db")
    service = PeopleSyncService(
        projector=history,
        db_path=directory.db_path,
        people=directory,
        backup_dir=tmp_path / "backups",
    )
    loop_thread = threading.get_ident()
    calls = []
    original = directory.refresh

    def refresh():
        calls.append(threading.get_ident())
        original()

    monkeypatch.setattr(directory, "refresh", refresh)
    try:
        assert await service.run_once() is not None
        assert calls == [loop_thread]
        index = directory._index()
        sql = []
        directory._local.state.connection.set_trace_callback(sql.append)
        assert directory._index() is index
        assert sql == ["PRAGMA data_version"]
        assert await service.run_once() is not None
        assert directory._index() is index
    finally:
        directory.close()
        history.close()
