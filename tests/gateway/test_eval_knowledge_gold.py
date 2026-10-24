"""Synthetic P0 contract: private gold, source-based scoring, immutable copies."""

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner
from yeoman_gateway.cli.commands import app
from yeoman_gateway.evaluation import knowledge_gold as gold
from yeoman_gateway.knowledge.models import KnowledgeContext, SourceRef


def item(**changes):
    value = dict(
        id="K1",
        question="Synthetic question",
        reader="owner_dm",
        person_ids=[1],
        case_type="current_state",
        expected_facts=[
            dict(statement="Synthetic fact", as_of="2026-10-24", source_message_ids=["m1"])
        ],
        expected_outcome="answer",
    )
    return value | changes


def document(**changes):
    return dict(schema_version=1, items=[item()]) | changes


@pytest.mark.parametrize(
    "change",
    [
        dict(schema_version=True),
        dict(schema_version=2),
        dict(extra="private"),
        dict(items=[]),
        dict(items=[item(id="bad id")]),
        dict(items=[item(reader="person_dm:no")]),
        dict(items=[item(person_ids=[True])]),
        dict(items=[item(case_type="unknown")]),
        dict(items=[item(expected_facts=[])]),
        dict(
            items=[
                item(
                    expected_facts=[
                        dict(statement="x", as_of="2026-02-30", source_message_ids=["m"])
                    ]
                )
            ]
        ),
        dict(items=[item(), item()]),
    ],
)
def test_schema_rejects_invalid_input(change):
    with pytest.raises(ValueError):
        gold.GoldFile.model_validate(document(**change))


@pytest.mark.parametrize("reader", ["owner_dm", "group:g@g.us", "person_dm:1", "non_member_dm"])
def test_schema_reader_kinds(reader):
    assert (
        gold.GoldFile.model_validate(document(items=[item(reader=reader)])).items[0].reader
        == reader
    )


def recalled(*ids):
    return KnowledgeContext(
        text="PRIVATE recalled statement" if ids else "",
        statement_ids=("s",) if ids else (),
        source_refs=tuple(SourceRef(i, 1, "whatsapp", "g@g.us", "whatsapp:1", 1) for i in ids),
    )


@pytest.mark.parametrize("outcome", ["refuse", "dont_know", "nothing_injected"])
def test_absence_cases_only_pass_when_empty(outcome):
    case = gold.GoldItem.model_validate(item(expected_outcome=outcome, expected_facts=[]))
    assert gold.score_item(case, recalled())["passed"] is True
    assert gold.score_item(case, recalled("unexpected"))["passed"] is False


def test_recall_scores_source_ids_not_text():
    case = gold.GoldItem.model_validate(item())
    assert gold.score_item(case, recalled("m1"))["facts_found"] == 1
    assert gold.score_item(case, recalled("other"))["facts_found"] == 0


def test_readonly_no_sidecars_or_changed_bytes(tmp_path):
    path = tmp_path / "copy.db"
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE synthetic (id)")
    conn.close()
    before = path.read_bytes()
    with gold.readonly_db(path) as conn:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO synthetic VALUES (1)")
    assert path.read_bytes() == before
    assert not Path(str(path) + "-wal").exists()
    assert not Path(str(path) + "-shm").exists()


def test_gold_paths_refuse_git_and_symlink(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: elsewhere")
    with pytest.raises(ValueError):
        gold.private_path(repo / "gold.json")
    link = tmp_path / "link"
    link.symlink_to(repo, target_is_directory=True)
    with pytest.raises(ValueError):
        gold.private_path(link / "gold.json")


def test_private_outputs(tmp_path):
    path = tmp_path / "report.json"
    gold.write_private(path, "content")
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        gold.write_private(path, "overwrite")


@pytest.mark.parametrize("flags", [["--grader-route", "test", "--dry-run"], []])
def test_cli_dry_run_never_builds_provider(tmp_path, monkeypatch, flags):
    from yeoman_shared.config import loader

    monkeypatch.setattr(loader, "load_config", lambda: pytest.fail("provider/config access"))
    path = tmp_path / "gold.json"
    path.write_text(json.dumps(document()))
    monkeypatch.setattr(gold, "recall_items", lambda *_args, **_kw: [recalled("m1")])
    result = CliRunner().invoke(
        app,
        [
            "eval",
            "knowledge-gold",
            "--gold",
            str(path),
            "--knowledge-db",
            str(tmp_path / "k.db"),
            "--people-db",
            str(tmp_path / "p.db"),
            "--history-db",
            str(tmp_path / "h.db"),
            "--out",
            str(tmp_path / "out"),
            *flags,
        ],
    )
    assert result.exit_code == 0, result.output
    report = json.loads((tmp_path / "out/report.json").read_text())
    assert report["metrics"]["gold_recall"] == 1
    assert report["metrics"]["answer_from_knowledge"] is None
    assert "PRIVATE" not in json.dumps(report)
    assert "Synthetic" not in json.dumps(report)


@pytest.mark.asyncio
async def test_grader_answer_is_generated_without_expected_facts():
    class Client:
        calls = []

        async def chat_with_usage(self, messages, **kwargs):
            from types import SimpleNamespace

            self.calls.append(messages)
            return SimpleNamespace(
                content="a synthetic answer" if len(self.calls) == 1 else '{"correct": true}'
            )

    client = Client()
    assert await gold.grade_item(client, gold.GoldItem.model_validate(item()), recalled("m1"))
    assert "Synthetic fact" not in json.dumps(client.calls[0])
    assert "Synthetic fact" in json.dumps(client.calls[1])


@pytest.fixture
def copies(tmp_path):
    from yeoman_gateway.history.queries import HistoryQueries
    from yeoman_gateway.history.reader import HistorySnapshot
    from yeoman_gateway.history.schema import create
    from yeoman_gateway.knowledge._history_sources import HistoryKnowledgeSources
    from yeoman_gateway.knowledge._history_upgrade import upgrade_history_knowledge
    from yeoman_gateway.knowledge.runtime import RuntimeKnowledgeSources

    from tests.gateway.convhist.test_hist_queries import contact, event, identifier, message
    from tests.gateway.people.resolver_fixtures import synced_directory
    from tests.gateway.test_history_knowledge_identity import capture, owner_dm_statement
    from tests.gateway.test_history_knowledge_upgrade import open_service, v2

    h = tmp_path / "history.db"
    db = sqlite3.connect(h)
    create(db)
    for pid, phone in [("a", "4915000000001"), ("b", "4915000000002"), ("c", "4915000000003")]:
        contact(db, pid)
        identifier(db, pid, phone + "@s.whatsapp.net", start=1)
    event(
        db,
        "roster",
        "member_snapshot",
        10,
        {
            "complete": True,
            "participants": [["4915000000001@s.whatsapp.net"], ["4915000000002@s.whatsapp.net"]],
        },
    )
    message(db, "m1", ms=100, identifier="4915000000001@s.whatsapp.net", text="Synthetic fact")
    db.commit()
    db.execute("UPDATE contacts SET role='owner' WHERE contact_id='b'")
    db.commit()
    snapshot = HistorySnapshot(1, (), db)
    queries = HistoryQueries(snapshot)
    directory = synced_directory(snapshot, tmp_path / "directory")
    p = directory.db_path
    directory.close()
    with sqlite3.connect(p) as conn:
        conn.execute(
            "UPDATE persons SET is_owner=1 WHERE id IN (SELECT person_id FROM "
            "person_ids WHERE kind='history_contact' AND value='b')"
        )
        person = conn.execute(
            "SELECT person_id FROM person_ids WHERE kind='history_contact' AND value='a'"
        ).fetchone()[0]
    conn.close()
    k = tmp_path / "knowledge.db"
    upgrade_history_knowledge(source=v2(tmp_path / "v2.db"), target=k)
    service = open_service(k)
    sources = HistoryKnowledgeSources(
        queries, {}, service.history_source_ledger, RuntimeKnowledgeSources()
    )
    with service.history_scope(queries, sources):
        capture(service, sources.issue("m1"))
        owner_dm_statement(service, sources, db)
        db.commit()
    service.close()
    snapshot.close()
    return k, p, h, person


@pytest.mark.parametrize("kind", ["owner_dm", "group:g@g.us", "person_dm", "non_member_dm"])
def test_real_recall_reader_context_and_immutable_copies(copies, kind):
    k, p, h, person = copies
    before = [path.read_bytes() for path in (k, p, h)]
    reader = f"person_dm:{person}" if kind == "person_dm" else kind
    case = gold.GoldFile.model_validate(
        document(items=[item(reader=reader, person_ids=[person], question="Synthetic statement")])
    )
    contexts = gold.recall_items(case, knowledge_db=k, people_db=p, history_db=h)
    assert len(contexts) == 1
    if kind in ("owner_dm", "group:g@g.us"):
        assert contexts[0].statement_ids
    else:
        assert contexts[0].empty
    assert [path.read_bytes() for path in (k, p, h)] == before
    for path in (k, p, h):
        assert not Path(str(path) + "-wal").exists()
        assert not Path(str(path) + "-shm").exists()


def test_template_no_content_and_case_slots(copies, tmp_path):
    _, p, h, person = copies
    path = tmp_path / "template.json"
    gold.make_template(people_db=p, history_db=h, persons=[person], out=path)
    payload = json.loads(path.read_text())
    assert {i["case_type"] for i in payload["items"]} == set(gold.CASE_TYPES)
    assert all(i["expected_facts"] == [] for i in payload["items"])
    assert "Synthetic" not in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600


def test_non_member_is_proven_directory_person(copies):
    from yeoman_gateway.history.queries import HistoryQueries
    from yeoman_gateway.history.reader import HistorySnapshot

    _, p, h, person = copies
    with gold.readonly_db(p) as people, gold.readonly_db(h) as history:
        context = gold.build_reader(
            gold.GoldItem.model_validate(item(reader="non_member_dm", person_ids=[person])),
            people=people,
            queries=HistoryQueries(HistorySnapshot(1, (), history)),
            now_ms=1000,
        )
        assert HistoryQueries(HistorySnapshot(1, (), history)).principal_contact(
            context.principal_id, 1000
        )
        assert (
            context.principal_id
            not in HistoryQueries(HistorySnapshot(1, (), history))
            .members(chat_id="g@g.us", at_ms=1000)
            .members
        )


def test_group_reader_uses_owner_endpoint_that_is_a_member():
    from types import SimpleNamespace

    people = sqlite3.connect(":memory:")
    people.row_factory = sqlite3.Row
    people.execute("CREATE TABLE persons (id INTEGER, is_owner INTEGER, merged_into INTEGER)")
    people.execute(
        "CREATE TABLE person_ids (person_id INTEGER, kind TEXT, value TEXT, hidden INTEGER, "
        "valid_from_ms INTEGER, valid_until_ms INTEGER)"
    )
    people.execute("INSERT INTO persons VALUES (1, 1, NULL), (2, 0, NULL)")
    people.executemany(
        "INSERT INTO person_ids VALUES (1, 'phone', ?, 0, NULL, NULL)",
        [("4915000000001",), ("4915000000002",)],
    )
    member = "whatsapp:4915000000002"
    queries = SimpleNamespace(
        principal_contact=lambda principal, at_ms: object(),
        members=lambda chat_id, at_ms: SimpleNamespace(status="known", members=frozenset({member})),
    )
    context = gold.build_reader(
        gold.GoldItem.model_validate(item(reader="group:g@g.us", person_ids=[2])),
        people=people,
        queries=queries,
        now_ms=1000,
    )
    assert context.principal_id == member
    assert context.chat_id == "g@g.us"


@pytest.mark.asyncio
async def test_metric_denominator_and_content_free_model_report(tmp_path):
    class Client:
        async def chat_with_usage(self, messages, **kwargs):
            from types import SimpleNamespace

            return SimpleNamespace(
                content='{"correct": true}'
                if messages[0]["content"] == gold.GRADER_PROMPT
                else "PRIVATE answer"
            )

    cases = gold.GoldFile.model_validate(
        document(items=[item(), item(id="K2", expected_outcome="refuse", expected_facts=[])])
    )
    report = await gold.evaluate(cases, [recalled("m1"), recalled("leak")], client=Client())
    assert report["metrics"]["answer_from_knowledge"] == 1
    assert report["metrics"]["privacy_leaks"] == 1
    assert report["metrics"]["refusal_correctness"] == 0
    out = tmp_path / "out"
    gold.write_outputs(out, report)
    for path in out.iterdir():
        assert path.stat().st_mode & 0o777 == 0o600
        assert "PRIVATE" not in path.read_text() and "Synthetic" not in path.read_text()


def test_readonly_rejects_wal_before_open(tmp_path):
    path = tmp_path / "copy.db"
    path.touch()
    Path(str(path) + "-wal").touch()
    with pytest.raises(ValueError):
        with gold.readonly_db(path):
            pytest.fail("must not open incomplete copy")


@pytest.mark.asyncio
async def test_grader_failure_cannot_count_as_correct_refusal():
    class Client:
        async def chat_with_usage(self, messages, **kwargs):
            from types import SimpleNamespace

            return SimpleNamespace(
                content='{"correct": false}'
                if messages[0]["content"] == gold.GRADER_PROMPT
                else "unsupported answer"
            )

    cases = gold.GoldFile.model_validate(
        document(items=[item(expected_outcome="refuse", expected_facts=[])])
    )
    report = await gold.evaluate(cases, [recalled()], client=Client())
    assert report["metrics"]["refusal_correctness"] == 0


def test_cli_runs_real_isolated_copies_and_template(copies, tmp_path):
    k, p, h, person = copies
    path = tmp_path / "gold.json"
    path.write_text(
        json.dumps(
            document(
                items=[
                    item(reader="group:g@g.us", person_ids=[person], question="Synthetic statement")
                ]
            )
        )
    )
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "eval",
            "knowledge-gold",
            "--gold",
            str(path),
            "--knowledge-db",
            str(k),
            "--people-db",
            str(p),
            "--history-db",
            str(h),
            "--out",
            str(tmp_path / "report"),
        ],
    )
    assert result.exit_code == 0, result.output
    report = json.loads((tmp_path / "report/report.json").read_text())
    assert report["metrics"]["gold_recall"] == 1
    result = runner.invoke(
        app,
        [
            "eval",
            "knowledge-gold-template",
            "--people-db",
            str(p),
            "--history-db",
            str(h),
            "--persons",
            str(person),
            "--out",
            str(tmp_path / "template.json"),
        ],
    )
    assert result.exit_code == 0, result.output


def test_legacy_source_alias_scores_history_message_id(tmp_path):
    from yeoman_gateway.knowledge._history_sources import HistorySourceAlias
    from yeoman_gateway.knowledge._history_upgrade import upgrade_history_knowledge
    from yeoman_gateway.knowledge.api import history_message_ids
    from yeoman_gateway.knowledge.authority import EvidenceAudience

    from tests.gateway.test_history_knowledge_upgrade import open_service, v2

    k = tmp_path / "knowledge.db"
    upgrade_history_knowledge(source=v2(tmp_path / "v2.db"), target=k)
    service = open_service(k)
    source = SourceRef("legacy-event", 7, "whatsapp", "g@g.us", "whatsapp:1", 1)
    alias = HistorySourceAlias(
        source, "history-message", 1, "a", "fingerprint", EvidenceAudience.author_only()
    )
    service.history_source_ledger.persist_aliases({source.key: alias})
    assert history_message_ids(service, [source]) == {"history-message"}
    service.close()
