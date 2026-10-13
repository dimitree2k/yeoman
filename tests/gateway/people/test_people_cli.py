from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner
from yeoman_gateway.cli.commands import app
from yeoman_gateway.people.schema import open_people_db

from tests.gateway.people.people_fixtures import level_history
from tests.gateway.people.test_knowledge_import import make_knowledge_fixture

runner = CliRunner()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def people_db(home):
    return home / "data" / "people" / "people.db"


def test_cli_check_exit_code(home):
    conn = open_people_db(people_db(home))
    conn.execute("INSERT INTO persons DEFAULT VALUES")
    conn.execute("INSERT INTO persons DEFAULT VALUES")
    conn.execute("UPDATE persons SET merged_into = 2 WHERE id = 1")
    conn.execute("UPDATE persons SET merged_into = 1 WHERE id = 2")
    conn.close()
    result = runner.invoke(app, ["people", "check", "--json"])
    assert result.exit_code == 1 and json.loads(result.stdout)[0]["code"] == "merge_cycle"


def test_cli_check_clean(home):
    open_people_db(people_db(home)).close()
    result = runner.invoke(app, ["people", "check"])
    assert result.exit_code == 0 and "clean" in result.stdout


def test_cli_sync_dry_run_never_writes(home, tmp_path):
    history = level_history(tmp_path / "history")
    history.close()
    result = runner.invoke(app, ["people", "sync", "--dry-run", "--history-db", str(history.path), "--json"])
    assert result.exit_code == 0, result.output
    assert not people_db(home).exists()
    data = json.loads(result.stdout)
    assert data["report"]["persons_created"] == 8
    assert data["status"]["persons"] == {"contact": 6, "seen": 2}
    assert "Alex" not in result.stdout


def test_cli_sync_requires_dry_run(home):
    assert runner.invoke(app, ["people", "sync"]).exit_code == 2


def test_cli_status_after_sync(home, tmp_path):
    from yeoman_gateway.people.history_source import read_observed_persons
    from yeoman_gateway.people.store import PeopleStore
    from yeoman_gateway.people.sync import sync_from_history

    history = level_history(tmp_path / "history")
    store = PeopleStore(open_people_db(people_db(home)))
    with store.transaction("sync"):
        sync_from_history(store, read_observed_persons(history.snapshot().connection), 1)
    store.conn.close()
    history.close()
    result = runner.invoke(app, ["people", "status", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["persons"]["contact"] == 6 and data["ids"]["history_contact:active"] == 9


def test_cli_import_dry_run_then_confirm_snapshots_first(home, tmp_path):
    knowledge = make_knowledge_fixture(tmp_path)
    open_people_db(people_db(home)).close()
    dry = runner.invoke(app, ["people", "import-knowledge", "--dry-run", "--knowledge-db", str(knowledge), "--json"])
    assert dry.exit_code == 0, dry.output
    assert json.loads(dry.stdout)["report"]["created"] == 2
    conn = open_people_db(people_db(home))
    assert conn.execute("SELECT count(*) FROM persons").fetchone()[0] == 0
    conn.close()

    result = runner.invoke(app, ["people", "import-knowledge", "--confirm", "--knowledge-db", str(knowledge), "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["snapshot"] is True and data["report"]["owner_nicknames"] == 1
    snapshots = list((home / "backups" / "people").iterdir())
    assert len(snapshots) == 1 and "people-pre-import" in snapshots[0].name
    conn = open_people_db(people_db(home))
    assert conn.execute("SELECT count(*) FROM persons").fetchone()[0] == 2
    conn.close()


def test_cli_import_needs_exactly_one_mode(home, tmp_path):
    knowledge = make_knowledge_fixture(tmp_path)
    assert runner.invoke(app, ["people", "import-knowledge", "--knowledge-db", str(knowledge)]).exit_code == 2
