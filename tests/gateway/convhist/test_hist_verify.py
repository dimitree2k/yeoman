import json
import sqlite3

import pytest
from hist_fixtures import _bf, sample_layer1, write_jsonl
from typer.testing import CliRunner
from yeoman_gateway.cli.commands import app
from yeoman_gateway.history.project import project
from yeoman_gateway.history.verify import table_digest, verify

runner = CliRunner()


def test_verify_reports(tmp_path):
    live, dev = sample_layer1(tmp_path)
    db = tmp_path / "out" / "history.db"
    project([live, dev], db)
    report = verify([live, dev], db, scratch=tmp_path / "scratch", frozen=True)
    cov = report["coverage"]
    assert (cov["messages_with_identifier"], cov["with_contact"], cov["with_confirmed_contact"]) == (3, 3, 2)
    assert cov["meets_95_percent"] is False
    assert [p["identifiers"] for p in cov["provisional_contacts"]] == [["4915253696948"]]
    assert cov["unresolved_messages"] == []
    assert report["accounting_ok"] is True and report["deterministic"] is True
    assert report["digests"] == table_digest(db)


def test_verify_review_keeps_unmatched_purged_events(tmp_path):
    live, dev = sample_layer1(tmp_path)
    write_jsonl(dev / "backfill/unmatched.jsonl", [
        _bf("journal", "reaction", {"targetMessageId": "missing", "senderId": "4915253696948"})
    ])
    db = tmp_path / "out" / "history.db"
    built = project([live, dev], db)
    report = verify([live, dev], db, scratch=None)
    assert built["review"]["unmatched_event_payloads"]
    assert report["review"]["unmatched_event_payloads"] == built["review"]["unmatched_event_payloads"]


def test_cli_project_and_verify(tmp_path):
    live, dev = sample_layer1(tmp_path)
    db = tmp_path / "out" / "history.db"
    built = runner.invoke(app, ["history", "project", "--layer1", str(live), "--layer1", str(dev), "--db", str(db)])
    assert built.exit_code == 0, built.output
    assert json.loads(built.output)["messages"] == 4
    checked = runner.invoke(app, ["history", "verify", "--layer1", str(live), "--layer1", str(dev), "--db", str(db)])
    assert checked.exit_code == 0, checked.output
    assert json.loads(checked.output)["accounting_ok"] is True
    assert json.loads(checked.output)["boundary"]["mode"] == "live"
    frozen = runner.invoke(app, ["history", "verify", "--layer1", str(live), "--layer1", str(dev),
                                "--db", str(db), "--frozen"])
    assert frozen.exit_code == 0, frozen.output
    assert json.loads(frozen.output)["boundary"]["mode"] == "frozen"
    scratch = tmp_path / "cli-scratch"
    refused = runner.invoke(app, ["history", "verify", "--layer1", str(dev), "--db", str(db),
                                 "--scratch", str(scratch)])
    assert refused.exit_code != 0 and not scratch.exists()


def test_verify_live_wal_without_immutable(tmp_path):
    import sqlite3

    import pytest
    from yeoman_gateway.history.verify import _open

    live, dev = sample_layer1(tmp_path)
    db = tmp_path / 'history.db'
    project([live, dev], db)
    writer = sqlite3.connect(db)
    try:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.row_factory = sqlite3.Row
        row = dict(writer.execute("SELECT * FROM messages WHERE sender_identifier IS NOT NULL LIMIT 1").fetchone())
        row['message_id'] = 'wal-new-message'
        writer.execute(f"INSERT INTO messages ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})",
                       tuple(row.values()))
        writer.commit()
        before = {p: p.read_bytes() for p in [db, db.with_name(db.name + '-wal')]}
        report = verify([live, dev], db, scratch=None)
        assert report['coverage']['messages_with_identifier'] == 4
        assert report['boundary']['mode'] == 'live'
        assert report['boundary']['tail_freshness'] is False
        assert before == {p: p.read_bytes() for p in before}
        conn = _open(db)
        try:
            with pytest.raises(sqlite3.OperationalError):
                conn.execute('DELETE FROM messages')
        finally:
            conn.close()
        assert verify([live, dev], db, scratch=None, frozen=True)['coverage']['messages_with_identifier'] == 3
    finally:
        writer.close()


def test_verify_determinism_requires_pinned_inputs(tmp_path, monkeypatch):
    import importlib

    import pytest

    module = importlib.import_module('yeoman_gateway.history.verify')
    live, dev = sample_layer1(tmp_path)
    db = tmp_path / 'history.db'
    project([live, dev], db)
    write_jsonl(dev / 'backfill/tombstone.jsonl', [{'purged_version': 1}])
    with (dev / 'backfill/tombstone.jsonl').open('a') as out:
        out.write('\n')
    projected = project([live, dev], db)
    original = db.read_bytes()
    checked = verify([live, dev], db, scratch=None, frozen=True)
    assert checked['accounting']['backfill/tombstone.jsonl'] == {'lines': 2, 'accounted': 2}
    import sqlite3
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT lines FROM projector_state WHERE file='backfill/tombstone.jsonl'").fetchone() == (2,)
    assert projected['projector_state_line_basis'] == 'physical'
    assert projected['accounting'] == checked['accounting']
    assert projected['blank_lines_skipped'] == checked['blank_lines_skipped']
    assert projected['outcomes']['backfill/tombstone.jsonl'] == {'skipped:purged': 1, 'skipped:blank': 1}
    assert checked['blank_lines_skipped']['backfill/tombstone.jsonl'] == 1
    assert checked['accounting_ok'] is True
    with pytest.raises(ValueError, match='frozen'):
        verify([live, dev], db, scratch=tmp_path / 'live-scratch')
    real_project = module.project
    calls = []

    def grow(roots, target):
        calls.append(target)
        result = real_project(roots, target)
        with (dev / 'backfill/growing.jsonl').open('a') as out:
            out.write('{"purged_version":1}\n')
        return result

    monkeypatch.setattr(module, 'project', grow)
    with pytest.raises(ValueError, match='changed'):
        verify([live, dev], db, scratch=tmp_path / 'scratch', frozen=True)
    assert len(calls) == 1
    assert db.read_bytes() == original


def test_project_failure_preserves_existing_database(tmp_path, monkeypatch):
    import importlib

    import pytest

    module = importlib.import_module('yeoman_gateway.history.project')
    live, dev = sample_layer1(tmp_path)
    db = tmp_path / 'history.db'
    project([live, dev], db)
    before = db.read_bytes()

    def fail(*args, **kwargs):
        raise RuntimeError('synthetic build failure')

    monkeypatch.setattr(module, 'create', fail)
    with pytest.raises(RuntimeError, match='synthetic'):
        project([live, dev], db)
    assert db.read_bytes() == before
    assert not db.with_name(db.name + ".building").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', ['offset', 'missing', 'extra', 'hash', 'fk', 'corrupt', 'columns', 'runtime', 'input'])
async def test_rebuild_candidate_checkpoint_and_integrity_mismatch_refuses_replace(tmp_path, damage, monkeypatch):
    from test_hist_live import projector_fixture, start_ready
    from yeoman_gateway.history import live
    root, db, archive, p = projector_fixture(tmp_path)
    await start_ready(p)
    before, generation = db.read_bytes(), p.health()['generation']
    original = live.verify_rebuild_candidate
    replacements = []
    replace = p._replace_candidate
    def spy(candidate):
        replacements.append(candidate)
        return replace(candidate)
    monkeypatch.setattr(p, '_replace_candidate', spy)
    def corrupt(roots, candidate, **kwargs):
        if damage == 'corrupt':
            candidate.write_bytes(b'not sqlite')
        elif damage == 'input':
            with (roots[0] / 'whatsapp/2026-10.jsonl').open('ab') as out:
                out.write(b'\n')
        else:
            with sqlite3.connect(candidate) as conn:
                sql = {'offset': "UPDATE projector_state SET end_offset=end_offset+1 WHERE file!='@runtime'",
                       'missing': "DELETE FROM projector_state WHERE file='whatsapp/2026-10.jsonl'",
                       'extra': "INSERT INTO projector_state VALUES ('whatsapp/extra.jsonl',0,0,'',3,'{}')",
                       'hash': "UPDATE projector_state SET sha256='bad' WHERE file!='@runtime'",
                       'fk': "UPDATE messages SET sender_contact_id='absent'",
                       'columns': 'ALTER TABLE messages ADD COLUMN unexpected TEXT',
                       'runtime': "UPDATE projector_state SET state_json='{}' WHERE file='@runtime'"}[damage]
                conn.execute(sql)
        return original(roots, candidate, **kwargs)
    monkeypatch.setattr(live, 'verify_rebuild_candidate', corrupt)
    try:
        with pytest.raises((ValueError, sqlite3.Error)):
            await p.rebuild(reason='owner_request')
        assert not replacements
        assert db.read_bytes() == before and p.health()['generation'] == generation
        assert p.health()['status'] == 'failed'
        with pytest.raises(live.HistoryPaused):
            await p.read_turn()
    finally:
        await p.stop()


def test_rebuild_candidate_expected_digests_are_independent(tmp_path):
    from test_hist_live import fixture
    from yeoman_gateway.history.verify import verify_rebuild_candidate
    from yeoman_shared.raw_archive.records import enumerate_committed
    root, db, _ = fixture(tmp_path, 2)
    report = verify_rebuild_candidate([root], db, boundaries=enumerate_committed(root))
    assert report['verified'] and report['accounting_ok']
    assert report['expected_digests'] == report['candidate_digests'] == table_digest(db)
    with sqlite3.connect(db) as conn:
        conn.execute('DELETE FROM messages WHERE message_id=(SELECT message_id FROM messages LIMIT 1)')
    with pytest.raises(ValueError, match='semantic_digest_mismatch'):
        verify_rebuild_candidate([root], db, boundaries=enumerate_committed(root))


@pytest.mark.parametrize('damage', ['outcomes', 'schema_version', 'admission'])
def test_rebuild_candidate_rejects_invalid_runtime_accounting(tmp_path, damage):
    from test_hist_live import fixture
    from yeoman_gateway.history.verify import verify_rebuild_candidate
    from yeoman_shared.raw_archive.records import enumerate_committed
    root, db, _ = fixture(tmp_path)
    with sqlite3.connect(db) as conn:
        if damage == 'schema_version':
            conn.execute('PRAGMA user_version=2')
        else:
            state = json.loads(conn.execute("SELECT state_json FROM projector_state WHERE file='@runtime'").fetchone()[0])
            if damage == 'outcomes':
                state['outcomes'] = {}
            else:
                state['admission'] = 'unknown'
            conn.execute("UPDATE projector_state SET state_json=? WHERE file='@runtime'", (json.dumps(state),))
    with pytest.raises(ValueError):
        verify_rebuild_candidate([root], db, boundaries=enumerate_committed(root))
