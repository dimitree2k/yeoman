from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner
from yeoman_gateway.cli.commands import app
from yeoman_gateway.people.check import check_people_db
from yeoman_gateway.people.schema import open_people_db
from yeoman_gateway.people.store import PeopleStore
from yeoman_gateway.people.suggestions import SuggestionError, decide_suggestion, list_suggestions
from yeoman_gateway.people.sync import sync_from_history

NOW = 1_791_000_000_000
runner = CliRunner()


@pytest.fixture
def store(tmp_path):
    conn = open_people_db(tmp_path / "people.db")
    yield PeopleStore(conn)
    conn.close()


def two_persons_with_suggestion(store: PeopleStore, kind: str = "merge") -> tuple[int, int, int]:
    with store.transaction("sync"):
        a = store.create_person(level="contact", now_ms=NOW)
        b = store.create_person(level="contact", now_ms=NOW)
        if kind == "merge":
            store.suggest("merge", b, a, evidence={"source": "knowledge_seed"})
        else:
            store.suggest(kind, a, None, field="home_city", value="Berlin")
    sid = store.conn.execute("SELECT id FROM suggestions").fetchone()[0]
    return a, b, sid


def decide(store, sid, **kw):
    with store.transaction("owner"):
        return decide_suggestion(store, sid, now_ms=NOW, note=kw.pop("note", None), **kw)


def test_list_shows_open_suggestions_with_persons(store):
    a, b, sid = two_persons_with_suggestion(store)
    [item] = list_suggestions(store.conn)
    assert item["id"] == sid and item["evidence"] == {"source": "knowledge_seed"}
    assert (item["person_a"]["id"], item["person_b"]["id"]) == (a, b)


def test_accept_merge_records_same_and_merges(store):
    a, b, sid = two_persons_with_suggestion(store)
    result = decide(store, sid, accept=True, note="same Telegram account")
    assert result == {"id": sid, "kind": "merge", "status": "accepted", "merged_into": a}
    assert store.terminal(b) == a and store.person_rows(b)["merged_by"] == "owner"
    assert store.latest_decision(a, b) == "same"
    assert list_suggestions(store.conn) == []
    assert check_people_db(store.conn) == []
    writers = {r[0] for r in store.conn.execute("SELECT DISTINCT writer FROM change_log WHERE tbl "
                                                "IN ('person_decisions', 'suggestions')")}
    assert "owner" in writers


def test_accepted_merge_survives_sync(store):
    a, b, sid = two_persons_with_suggestion(store)
    decide(store, sid, accept=True)
    with store.transaction("sync"):
        sync_from_history(store, [], NOW + 1)
    assert store.terminal(b) == a


def test_reject_merge_records_not_same(store):
    a, b, sid = two_persons_with_suggestion(store)
    result = decide(store, sid, accept=False)
    assert result["status"] == "rejected" and result["merged_into"] is None
    assert store.latest_decision(a, b) == "not_same" and store.terminal(b) == b
    assert not store.suggest("merge", a, b)          # never proposed again


def test_decided_suggestion_cannot_be_decided_again(store):
    _, _, sid = two_persons_with_suggestion(store)
    decide(store, sid, accept=False)
    with pytest.raises(SuggestionError, match="already rejected"):
        decide(store, sid, accept=True)


def test_unknown_suggestion(store):
    with pytest.raises(SuggestionError, match="no suggestion"):
        decide(store, 99, accept=True)


def test_accept_detail_not_supported_but_reject_works(store):
    _, _, sid = two_persons_with_suggestion(store, kind="detail")
    with pytest.raises(SuggestionError, match="not supported"):
        decide(store, sid, accept=True)
    assert decide(store, sid, accept=False)["status"] == "rejected"
    assert store.conn.execute("SELECT count(*) FROM person_decisions").fetchone()[0] == 0


def test_blocked_accept_rolls_back(store):
    a, b, sid = two_persons_with_suggestion(store)
    with store.transaction("sync"):
        c = store.create_person(level="contact", now_ms=NOW)
        store.merge(c, b, by="sync")
    store.conn.execute("INSERT INTO person_decisions(person_a, person_b, decision) "
                       "VALUES (?, ?, 'not_same')", (a, c))
    with pytest.raises(SuggestionError, match="blocked"):
        decide(store, sid, accept=True)
    assert store.terminal(b) == b
    assert store.latest_decision(a, b) is None
    assert store.conn.execute("SELECT status FROM suggestions WHERE id = ?", (sid,)).fetchone()[0] == "open"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def test_cli_list_accept_snapshots_first(home):
    store = PeopleStore(open_people_db(home / "data" / "people" / "people.db"))
    a, b, sid = two_persons_with_suggestion(store)
    store.conn.close()
    listed = runner.invoke(app, ["people", "suggestions", "--json"])
    assert listed.exit_code == 0, listed.output
    assert [s["id"] for s in json.loads(listed.stdout)] == [sid]

    result = runner.invoke(app, ["people", "accept", str(sid), "--note", "same person", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["snapshot"] is True and data["status"] == "accepted" and data["merged_into"] == a
    snapshots = list((home / "backups" / "people").iterdir())
    assert len(snapshots) == 1 and "people-pre-decision" in snapshots[0].name

    again = runner.invoke(app, ["people", "reject", str(sid)])
    assert again.exit_code == 1 and "already accepted" in again.output
    assert json.loads(runner.invoke(app, ["people", "suggestions", "--json"]).stdout) == []
    assert len(json.loads(runner.invoke(app, ["people", "suggestions", "--all", "--json"]).stdout)) == 1
