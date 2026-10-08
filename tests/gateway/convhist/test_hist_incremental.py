"""Physical checkpoints and Task 3 incremental engine witnesses."""
import hashlib
import json
import sqlite3
from contextlib import closing

import pytest
from hist_fixtures import T0, _bf, _raw, write_jsonl
from yeoman_gateway.history.attestations import make, write_seed
from yeoman_gateway.history.incremental import ProjectionIndex, RebuildRequired, apply_committed
from yeoman_gateway.history.layer1 import canonical_json
from yeoman_gateway.history.project import project
from yeoman_gateway.history.verify import table_digest, verify
from yeoman_shared.raw_archive.records import SourceBoundary, append_line, enumerate_committed


def test_checkpoint_uses_physical_bytes_lines_and_prefix_hash(tmp_path):
    root = tmp_path / "raw"
    source = root / "whatsapp/2026-10.jsonl"
    source.parent.mkdir(parents=True)
    native = _raw("message", "message", {
        "chatJid": "synthetic@g.us", "messageId": "utf8",
        "senderId": "97001@lid", "text": "Grüße 🐎", "timestamp": 1791000000})
    data = (json.dumps(native, ensure_ascii=False) + '\n\n{malformed\n{"purged_version":1}\n').encode()
    source.write_bytes(data)
    db = tmp_path / "history.db"
    report = project([root], db, publish_lineage_root=root)
    with closing(sqlite3.connect(db)) as conn:
        columns = [r[1] for r in conn.execute("PRAGMA table_info(projector_state)")]
        assert columns == ["file", "lines", "end_offset", "sha256", "projector_version", "state_json"]
        row = conn.execute("SELECT lines, end_offset, sha256, projector_version, state_json "
                           "FROM projector_state WHERE file='whatsapp/2026-10.jsonl'").fetchone()
        assert row[:4] == (4, len(data), hashlib.sha256(data).hexdigest(), 3)
        assert json.loads(row[4]) == {}
        runtime = conn.execute("SELECT lines, end_offset, sha256, projector_version, state_json "
                               "FROM projector_state WHERE file='@runtime'").fetchone()
        assert runtime[:4] == (0, 0, hashlib.sha256(b"").hexdigest(), 3)
        state = json.loads(runtime[4])
        assert state["generation"] == 1 and state["status"] == "ready"
        assert state["pending_pairs"] == {}
        assert state["outcomes"] == report["outcomes"]
        assert state["review"] == {key: len(items) for key, items in report["review"].items()}
        assert "Grüße" not in runtime[4] and "native" not in state
    assert report["projector_state_line_basis"] == "physical"
    assert report["blank_lines_skipped"] == {"whatsapp/2026-10.jsonl": 1, "derived/contact-ids.jsonl": 0}
    assert report["accounting"] == {"whatsapp/2026-10.jsonl": {"lines": 4, "accounted": 4},
                                    "derived/contact-ids.jsonl": {"lines": 1, "accounted": 1}}
    assert report["outcomes"]["whatsapp/2026-10.jsonl"] == {
        "message": 1, "invalid_json": 1, "skipped:blank": 1, "skipped:purged": 1}
    assert report["accounting_ok"]
    checked = verify([root], db, scratch=None)
    assert checked["accounting"] == report["accounting"] and checked["accounting_ok"]


def test_full_checkpoint_pending_pairs_contains_only_refs(tmp_path):
    from hist_fixtures import write_jsonl
    from yeoman_gateway.history.layer1 import canonical_json

    root = tmp_path / "raw"
    write_jsonl(root / "whatsapp/2026-10.jsonl", [
        _raw("outbound_request", "send_text", {"to": "synthetic@g.us", "text": "private fixture body"},
             direction="out", corr="missing"),
        _raw("outbound_result", "send_text", {}, direction="out", corr="reverse"),
    ])
    db = tmp_path / "history.db"
    project([root], db)
    with closing(sqlite3.connect(db)) as conn:
        raw_state = conn.execute("SELECT state_json FROM projector_state WHERE file='@runtime'").fetchone()[0]
        assert json.loads(raw_state)["pending_pairs"] == {
            canonical_json(["default", "missing"]): ["whatsapp/2026-10.jsonl#1"],
            canonical_json(["default", "reverse"]): ["whatsapp/2026-10.jsonl#2"],
        }
        assert "private fixture body" not in raw_state
        assert conn.execute("SELECT count(*) FROM messages").fetchone() == (0,)


def test_full_checkpoint_rejects_partial_tail_without_replacing_db(tmp_path):
    import pytest
    from hist_fixtures import write_jsonl

    root = tmp_path / "raw"
    source = write_jsonl(root / "whatsapp/2026-10.jsonl", [{"purged_version": 1}])
    db = tmp_path / "history.db"
    project([root], db)
    before = db.read_bytes()
    with source.open("ab") as handle:
        handle.write(b'{"partial":')
    original = source.read_bytes()
    with pytest.raises(ValueError, match="incomplete Layer 1 tail"):
        project([root], db)
    assert source.read_bytes() == original and db.read_bytes() == before
    assert not db.with_name(db.name + ".building").exists()


# Task 3 harness: Task 7 can replace restart/repair with its real live fence.

G = 'synthetic@g.us'
PN = '4915550000001@s.whatsapp.net'
OTHER = '4915550000002@s.whatsapp.net'


def observation(kind='message', native_id='M', *, text='original', ms=T0, sender=PN, chat=G):
    payload = {'chatJid': chat, 'senderId': sender, 'timestamp': ms,
               'messageId': native_id, 'text': text}
    if kind == 'reaction':
        payload.update(targetMessageId=native_id, emoji='🐎')
    return _raw(kind, kind, payload, received=ms)


def paired(kind, *, corr='pair', native_id='S', ok=True):
    row = _raw(kind, 'send_text', {'to': G, 'text': 'sent body'}, direction='out', corr=corr)
    row['chat_id'] = G
    if kind == 'outbound_result':
        row['native']['result'] = {'ok': ok, 'sent': {'providerMessageId': native_id}}
    return row


class EngineFixture:
    def __init__(self, out, *, frozen_shaped=False):
        self.root = out / 'raw'
        self.db = out / 'incremental.db'
        self.oracle = out / 'oracle.db'
        write_seed(self.root)
        write_jsonl(self.root / 'whatsapp/2026-01.jsonl', [observation(native_id='SEED')])
        if frozen_shaped:
            write_jsonl(self.root / 'backfill/knowledge.jsonl', [
                _bf('knowledge', 'contact_record', {'contactRef': '00000000-0000-4000-8000-000000000001', 'createdMs': 1,
                    'displayName': 'Synthetic person'}, channel='any'),
                _bf('knowledge', 'identifier_record', {'contactRef': '00000000-0000-4000-8000-000000000001', 'identifier': PN}),
            ])
            write_jsonl(self.root / 'backfill/reply_context.jsonl', [
                _bf('reply_context', 'message', {'messageId': 'SEED', 'senderId': PN, 'text': 'original'}, chat=G)])
        project([self.root], self.db, publish_lineage_root=self.root)
        self.conn = sqlite3.connect(self.db)
        self.conn.execute('PRAGMA foreign_keys=ON')
        self.restart()

    def close(self):
        self.conn.close()

    def boundaries(self):
        return tuple(SourceBoundary(*row) for row in self.conn.execute(
            "SELECT file, lines, end_offset, sha256 FROM projector_state WHERE file!='@runtime' ORDER BY file"))

    def restart(self):
        self.index = ProjectionIndex.from_prefix(self.root, self.boundaries())

    def append(self, row, rel='whatsapp/2026-01.jsonl'):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        append_line(path, canonical_json(row))
        return enumerate_committed(self.root)

    def apply(self, target=None):
        return apply_committed(self.conn, self.index, self.root,
                               target if target is not None else enumerate_committed(self.root))

    def state(self):
        return (table_digest(self.db), self.conn.execute(
            'SELECT * FROM projector_state ORDER BY file').fetchall(), self.current())

    def current(self):
        return self.conn.execute('SELECT * FROM messages_current ORDER BY message_id').fetchall()

    def parity(self):
        report = project([self.root], self.oracle)
        assert set(table_digest(self.oracle)) == {'contacts', 'identifier_history', 'messages', 'message_events'}
        assert table_digest(self.db) == table_digest(self.oracle)
        with closing(sqlite3.connect(self.oracle)) as conn:
            assert self.current() == conn.execute('SELECT * FROM messages_current ORDER BY message_id').fetchall()
            assert self.boundaries() == tuple(SourceBoundary(*row) for row in conn.execute(
                "SELECT file, lines, end_offset, sha256 FROM projector_state WHERE file!='@runtime' ORDER BY file"))
            actual = json.loads(self.conn.execute(
                "SELECT state_json FROM projector_state WHERE file='@runtime'").fetchone()[0])
            expected = json.loads(conn.execute(
                "SELECT state_json FROM projector_state WHERE file='@runtime'").fetchone()[0])
            assert actual['outcomes'] == expected['outcomes'] == report['outcomes']
            assert actual['pending_pairs'] == expected['pending_pairs']
            assert actual['review'] == expected['review']
        for table in ('contacts', 'messages', 'message_events'):
            for (refs,) in self.conn.execute(f'SELECT source_refs FROM {table}'):
                assert json.loads(refs) == sorted(set(json.loads(refs)))
        assert not self.conn.execute('PRAGMA foreign_key_check').fetchall()


@pytest.fixture
def engine(tmp_path, request):
    params = getattr(getattr(request.node, 'callspec', None), 'params', {})
    fixture = EngineFixture(tmp_path, frozen_shaped=params.get('case') == 'frozen_shaped')
    yield fixture
    fixture.close()


class CrashConnection:
    def __init__(self, conn, stage):
        self.conn, self.stage = conn, stage

    def __getattr__(self, name):
        return getattr(self.conn, name)

    def execute(self, sql, *args):
        if self.stage == 'before_rows' and sql.startswith('DELETE FROM message_events'):
            raise RuntimeError('crash')
        if self.stage == 'after_rows' and sql.startswith('INSERT INTO projector_state'):
            raise RuntimeError('crash')
        result = self.conn.execute(sql, *args)
        if self.stage == 'after_cursor' and sql.startswith('INSERT INTO projector_state'):
            raise RuntimeError('crash')
        return result

    def executemany(self, sql, *args):
        if self.stage == 'before_rows' and sql.startswith('DELETE FROM message_events'):
            raise RuntimeError('crash')
        return self.conn.executemany(sql, *args)

    def commit(self):
        if self.stage == 'before_commit':
            raise RuntimeError('crash')
        self.conn.commit()
        if self.stage == 'after_commit':
            raise RuntimeError('crash')


@pytest.mark.parametrize('stage', ['before_rows', 'after_rows', 'after_cursor', 'before_commit', 'after_commit'])
def test_incremental_crash_atomic_rows_and_checkpoint(engine, stage):
    before = engine.state()
    target = engine.append(observation(native_id='CRASH'))
    # A second destination shares the very same transaction.
    engine.append({'kind': 'media_description', 'native_message_id': 'CRASH',
                   'chat_id': G, 'text': 'description'}, 'derived/media-descriptions.jsonl')
    target = enumerate_committed(engine.root)
    with pytest.raises(RuntimeError, match='crash'):
        apply_committed(CrashConnection(engine.conn, stage), engine.index, engine.root, target)
    if stage != 'after_commit':
        assert engine.state() == before
    else:
        assert engine.boundaries() == target
        engine.parity()
    engine.restart()
    engine.apply(target)
    engine.parity()
    assert engine.boundaries() == target
    for table, key in [('messages', 'message_id'), ('message_events', 'event_id')]:
        assert engine.conn.execute(f'SELECT count(*)-count(DISTINCT {key}) FROM {table}').fetchone()[0] == 0


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('ok', [False, True])
def test_incremental_pairs_survive_restart_and_month_rollover(engine, reverse, ok):
    first, second = ('outbound_result', 'outbound_request') if reverse else ('outbound_request', 'outbound_result')
    engine.append(paired(first, ok=ok))
    engine.apply()
    state = json.loads(engine.conn.execute("SELECT state_json FROM projector_state WHERE file='@runtime'").fetchone()[0])
    assert state['pending_pairs'][canonical_json(['default', 'pair'])] == ['whatsapp/2026-01.jsonl#2']
    assert engine.conn.execute("SELECT count(*) FROM messages WHERE native_message_id='S'").fetchone()[0] == 0
    engine.restart()
    engine.append(paired(second, ok=ok), 'whatsapp/2026-02.jsonl')
    engine.apply()
    engine.parity()
    rows = engine.conn.execute("SELECT source_refs FROM messages WHERE native_message_id='S'").fetchall()
    assert len(rows) == int(ok)
    if ok:
        assert json.loads(rows[0][0]) == ['whatsapp/2026-01.jsonl#2', 'whatsapp/2026-02.jsonl#1']
    engine.restart()
    engine.append(paired(second, ok=ok), 'whatsapp/2026-03.jsonl')
    engine.apply()
    engine.parity()


@pytest.mark.parametrize('reverse', [False, True])
def test_incremental_late_evidence_retracts_obsolete_clusters_and_joins(engine, reverse):
    times = [T0, T0 + 100_000, T0 + 200_000]
    for ms in times[::-1] if reverse else times:
        engine.append(observation('reaction', ms=ms))
        engine.apply()
        engine.parity()
    old = {r[0] for r in engine.conn.execute('SELECT event_id FROM message_events')}
    engine.append(observation('reaction', ms=T0 - 50_000))
    engine.apply()
    engine.parity()
    assert old - {r[0] for r in engine.conn.execute('SELECT event_id FROM message_events')}
    engine.append(_bf('session_jsonl', 'message', {'text': 'join', 'senderId': PN}, chat=G),
                  'backfill/session_jsonl.jsonl')
    engine.apply()
    standalone = engine.conn.execute("SELECT message_id FROM messages WHERE text='join'").fetchone()[0]
    engine.append(observation(native_id='J1', text='join'))
    engine.apply()
    assert engine.conn.execute('SELECT count(*) FROM messages WHERE message_id=?', (standalone,)).fetchone()[0] == 0
    engine.parity()
    engine.append(observation(native_id='J2', text='join'))
    engine.apply()
    engine.parity()
    assert engine.conn.execute("SELECT count(*) FROM messages WHERE text='join'").fetchone()[0] == 3
    engine.append(paired('outbound_request'))
    engine.append(paired('outbound_result'))
    engine.apply()
    assert engine.conn.execute("SELECT count(*) FROM messages WHERE native_message_id='S'").fetchone()[0] == 1
    engine.restart()
    engine.append(paired('outbound_result', native_id='CONFLICT'))
    engine.apply()
    engine.parity()
    assert engine.conn.execute("SELECT count(*) FROM messages WHERE native_message_id IN ('S','CONFLICT')").fetchone()[0] == 0


def test_incremental_late_author_after_restart_removes_obsolete_event_ids(engine):
    # Distinct pre-existing bound contacts: corrections must change event grouping.
    engine.append(observation(native_id='OTHER', sender=OTHER))
    engine.append(observation('reaction', native_id='SEED'))
    engine.apply()
    engine.parity()
    old_sender = engine.conn.execute("SELECT sender_contact_id FROM messages WHERE native_message_id='SEED'").fetchone()[0]
    old_actor, old_event = engine.conn.execute('SELECT actor_contact_id, event_id FROM message_events').fetchone()
    corrected = engine.conn.execute("SELECT sender_contact_id FROM messages WHERE native_message_id='OTHER'").fetchone()[0]
    assert old_sender != corrected and old_actor != corrected
    engine.restart()
    before = engine.state()
    for ref in ['whatsapp/2026-01.jsonl#1', 'whatsapp/2026-01.jsonl#3']:
        engine.append(make('author', T0 + 1000, 'synthetic correction', source_ref=ref, anchor=OTHER),
                      'owner/attestations.jsonl')
    with pytest.raises(RebuildRequired):
        engine.apply()
    assert engine.state() == before
    project([engine.root], engine.oracle)
    with closing(sqlite3.connect(engine.oracle)) as conn:
        assert conn.execute("SELECT sender_contact_id, sender_identifier FROM messages WHERE native_message_id='SEED'").fetchone() == (corrected, PN)
        actor, native, eid, refs = conn.execute('SELECT actor_contact_id, actor_identifier, event_id, source_refs FROM message_events').fetchone()
        assert (actor, native) == (corrected, PN) and eid != old_event
        assert 'owner/attestations.jsonl#' in refs
        assert 'owner/attestations.jsonl#' in conn.execute("SELECT source_refs FROM messages WHERE native_message_id='SEED'").fetchone()[0]
    assert set(table_digest(engine.oracle)) == {'contacts', 'identifier_history', 'messages', 'message_events'}


@pytest.mark.parametrize('order', [('message', 'edit', 'delete'), ('edit', 'delete', 'message')])
def test_incremental_edit_delete_matches_full_in_both_arrival_orders(engine, order):
    for kind in order:
        if kind == 'message' and order[0] == 'edit':
            assert engine.conn.execute('SELECT target_message_id FROM message_events ORDER BY kind').fetchall() == [(None,), (None,)]
            engine.restart()
        engine.append(observation(kind, text='edited' if kind == 'edit' else 'original',
                                  ms=T0 + {'message': 0, 'edit': 1000, 'delete': 2000}[kind]))
        engine.apply()
        engine.parity()
    assert engine.conn.execute("SELECT current_text, deleted FROM messages_current WHERE native_message_id='M'").fetchone() == ('edited', 1)
    assert engine.conn.execute("SELECT count(*) FROM messages WHERE native_message_id='M'").fetchone()[0] == 1
    assert engine.conn.execute('SELECT kind, target_native_id, target_message_id FROM message_events ORDER BY kind').fetchall() == [
        ('delete', 'M', f'whatsapp:{G}:M'), ('edit', 'M', f'whatsapp:{G}:M')]


@pytest.mark.parametrize('case', ['frozen_shaped', 'append', 'restart', 'late_evidence', 'window',
                                 'merge', 'unmerge', 'month_rollover', 'wal', 'purge'])
def test_incremental_matches_full_rebuild_fixture_matrix(engine, case):
    if case in {'window', 'merge', 'unmerge', 'purge'}:
        if case != 'purge':
            engine.append(observation(native_id='BOUND-OTHER', sender=OTHER))
            engine.apply()
            engine.parity()
        before = engine.state()
        if case == 'purge':
            path = engine.root / 'whatsapp/2026-01.jsonl'
            path.write_text(canonical_json({'purged_version': 1}) + '\n')
        else:
            if case == 'window':
                row = make('identifier', T0, 'synthetic window', anchor=PN, identifier=OTHER,
                           valid_from_ms=T0, valid_until_ms=T0 + 1000)
            else:
                row = make(case, T0, 'synthetic decision', a=PN, b=OTHER)
            engine.append(row, 'owner/attestations.jsonl')
        with pytest.raises(RebuildRequired):
            engine.apply()
        assert engine.state() == before
        return  # Task 7 extends this branch with the real fenced repair.
    if case == 'wal':
        assert engine.conn.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
    if case == 'restart':
        engine.restart()
    if case == 'frozen_shaped':
        engine.append(_bf('memory', 'message', {'messageId': 'B', 'segments': [
            {'text': 'batch head', 'senderId': PN}, {'text': 'batch tail', 'senderId': PN, 'messageId': 'B'}]}, chat=G),
            'backfill/memory.jsonl')
    elif case == 'late_evidence':
        engine.append(observation('reaction', native_id='SEED'))
    else:
        engine.append(observation(native_id='APPEND'),
                      'whatsapp/2026-02.jsonl' if case == 'month_rollover' else 'whatsapp/2026-01.jsonl')
    report = engine.apply()
    assert report['accounting_ok']
    engine.parity()


@pytest.mark.parametrize('mutation', ['truncate', 'equal_size', 'tombstone', 'missing', 'inode'])
def test_prefix_truncation_or_mutation_requires_rebuild(engine, mutation):
    before = engine.state()
    path = engine.root / 'whatsapp/2026-01.jsonl'
    data = path.read_bytes()
    if mutation == 'truncate':
        path.write_bytes(data[:-1])
    elif mutation == 'equal_size':
        path.write_bytes(data.replace(b'original', b'mutation'))
    elif mutation == 'tombstone':
        path.write_text(canonical_json({'purged_version': 1}) + '\n')
    elif mutation == 'missing':
        path.unlink()
    else:
        replacement = path.with_suffix('.replacement')
        replacement.write_bytes(data)
        replacement.replace(path)
    with pytest.raises(RebuildRequired):
        engine.apply(engine.boundaries())
    assert engine.state() == before


def test_incremental_valid_suffix_exact_target_and_physical_accounting(engine):
    target = engine.append(observation(native_id='BOUND'))
    engine.append(observation(native_id='LATER'))
    engine.apply(target)
    assert engine.conn.execute("SELECT count(*) FROM messages WHERE native_message_id='LATER'").fetchone()[0] == 0
    engine.append(None)  # JSON scalar: invalid, accounted.
    path = engine.root / 'whatsapp/2026-01.jsonl'
    with path.open('ab') as out:
        out.write(b'\n{invalid\n{"purged_version":1}\n')
    engine.apply()
    engine.parity()


def test_incremental_unrelated_chat_is_not_normalized(engine, monkeypatch):
    import yeoman_gateway.history.incremental as incremental

    engine.append(observation(native_id='UNRELATED', chat='other@g.us'))
    engine.apply()
    original = incremental._messages
    seen = []

    def record(ex, *args):
        seen.extend(c.chat_id for c in ex.messages)
        return original(ex, *args)

    monkeypatch.setattr(incremental, '_messages', record)
    engine.append(observation(native_id='LOCAL'))
    engine.apply()
    assert seen and set(seen) == {G}
    engine.parity()


def test_incremental_plan_is_staged_and_rollback_can_retry_same_index(engine):
    before = engine.state()
    target = engine.append(observation(native_id='RETRY'))
    with pytest.raises(RuntimeError, match='crash'):
        apply_committed(CrashConnection(engine.conn, 'after_cursor'), engine.index, engine.root, target)
    assert engine.state() == before
    engine.apply(target)
    engine.parity()
    assert engine.conn.execute("SELECT count(*) FROM messages WHERE native_message_id='RETRY'").fetchone()[0] == 1


@pytest.mark.parametrize('version', ['schema', 'projector'])
def test_incremental_unsupported_version_refuses_without_mutation(engine, version):
    engine.append(observation(native_id='VERSION'))
    if version == 'schema':
        engine.conn.execute('PRAGMA user_version=2')
    else:
        engine.conn.execute('UPDATE projector_state SET projector_version=2')
    engine.conn.commit()
    before = engine.conn.execute('SELECT * FROM projector_state ORDER BY file').fetchall()
    with pytest.raises(RebuildRequired):
        engine.apply()
    assert engine.conn.execute('SELECT * FROM projector_state ORDER BY file').fetchall() == before
    assert engine.conn.execute("SELECT count(*) FROM messages WHERE native_message_id='VERSION'").fetchone()[0] == 0


def test_incremental_pending_groups_separate_accounts_and_retract_target_events(engine):
    for account in ('default', 'other'):
        for kind in ('outbound_request', 'outbound_result'):
            row = paired(kind, native_id=account)
            row['account'] = account
            engine.append(row)
            engine.apply()
    engine.append(observation('reaction', native_id='default'))
    engine.apply()
    engine.parity()
    engine.restart()
    engine.append(paired('outbound_result', native_id='conflict'))
    engine.apply()
    engine.parity()
    assert engine.conn.execute("SELECT native_message_id FROM messages WHERE direction='out'").fetchall() == [('other',)]
    assert engine.conn.execute('SELECT target_message_id FROM message_events').fetchall() == [(None,)]


def test_incremental_name_fallback_includes_every_matching_name_in_chat(engine):
    def named(native_id, sender):
        row = observation(native_id=native_id, sender=sender)
        row['native']['payload']['senderName'] = 'Shared name'
        return row
    engine.append(named('NAMED', PN))
    engine.append(named('UNKNOWN', None))
    engine.apply()
    known = engine.conn.execute("SELECT sender_contact_id FROM messages WHERE native_message_id='NAMED'").fetchone()[0]
    assert engine.conn.execute("SELECT sender_contact_id, sender_basis FROM messages WHERE native_message_id='UNKNOWN'").fetchone() == (known, 'push_name')
    engine.append(named('COMPETITOR', OTHER))
    engine.apply()
    engine.parity()
    assert engine.conn.execute("SELECT sender_contact_id, sender_basis FROM messages WHERE native_message_id='UNKNOWN'").fetchone() == (None, 'unknown')


def test_incremental_media_purged_payload_and_current_reactions(engine):
    engine.append(observation(native_id='PURGED', text=None))
    engine.apply()
    engine.append(_bf('session_jsonl', 'message', {'text': 'recovered', 'senderId': PN}, chat=G),
                  'backfill/session_jsonl.jsonl')
    engine.apply()
    engine.append({'kind': 'media_transcript', 'native_message_id': 'PURGED', 'chat_id': G,
                   'text': 'transcript', 'generated_ms': T0}, 'derived/media-transcripts.jsonl')
    engine.append(_bf('journal', 'media_record', {'messageId': 'PURGED', 'media': {'kind': 'audio'}}, chat=G),
                  'backfill/journal.jsonl')
    engine.apply()
    engine.parity()
    row = engine.conn.execute("SELECT text, media_json FROM messages WHERE native_message_id='PURGED'").fetchone()
    assert row[0] == 'recovered' and json.loads(row[1])['transcript']['text'] == 'transcript'
    for offset, emoji, removed in [(0, '🐎', False), (150_000, '🦄', False), (300_000, None, True)]:
        row = observation('reaction', native_id='PURGED', ms=T0 + offset)
        row['native']['payload'].update(emoji=emoji, removed=removed)
        engine.append(row)
        engine.apply()
        engine.parity()
    payloads = [json.loads(r[0]) for r in engine.conn.execute("SELECT payload_json FROM message_events WHERE kind='reaction'")]
    assert len(payloads) == 3 and not any(p['current'] for p in payloads)


def test_incremental_unanchored_assistant_reaction_attaches_within_ten_seconds(engine):
    echo = observation('reaction', native_id='SEED', sender=G)
    engine.append(echo)
    engine.apply()
    old = engine.conn.execute('SELECT event_id FROM message_events').fetchone()[0]
    purged = _bf('journal', 'reaction', {'fromAssistant': True, 'payloadPurged': True},
                 chat=G, ms=T0 + 9000)
    engine.append(purged, 'backfill/journal.jsonl')
    engine.apply()
    engine.parity()
    events = engine.conn.execute('SELECT event_id, source_refs FROM message_events').fetchall()
    assert len(events) == 1 and events[0][0] == old
    assert json.loads(events[0][1]) == ['backfill/journal.jsonl#1', 'whatsapp/2026-01.jsonl#2']


def test_incremental_component_join_and_global_numeric_change_require_rebuild(engine):
    engine.append(observation(native_id='SECOND', sender=OTHER))
    engine.apply()
    before = engine.state()
    row = observation(native_id='LINK')
    row['native']['payload'].update(participantJid='97001@lid', senderPhoneJid=PN)
    engine.append(row)
    # A new independently bound identifier is allowed until a join changes old ownership.
    try:
        engine.apply()
    except RebuildRequired:
        assert engine.state() == before
        return
    engine.parity()
    before = engine.state()
    row = observation(native_id='JOIN')
    row['native']['payload'].update(participantJid='97001@lid', senderPhoneJid=OTHER)
    engine.append(row)
    with pytest.raises(RebuildRequired):
        engine.apply()
    assert engine.state() == before


def test_incremental_equal_timestamp_sightings_keep_physical_first_ref(engine):
    for index in range(12):
        engine.append(observation(native_id=f'N{index}', sender=OTHER))
    engine.apply()
    engine.parity()
    contact = engine.conn.execute("SELECT sender_contact_id FROM messages WHERE native_message_id='N0'").fetchone()[0]
    refs = json.loads(engine.conn.execute('SELECT source_refs FROM contacts WHERE contact_id=?', (contact,)).fetchone()[0])
    assert 'whatsapp/2026-01.jsonl#2' in refs


def test_incremental_global_numeric_applicability_change_requires_rebuild(engine):
    engine.append(observation(native_id='NUMERIC', sender='97001'))
    engine.apply()
    engine.parity()
    before = engine.state()
    engine.append(observation(native_id='STRONG', sender='97001@lid', chat='other@g.us'))
    with pytest.raises(RebuildRequired):
        engine.apply()
    assert engine.state() == before


@pytest.mark.parametrize('bad_target', ['omit', 'duplicate', 'traversal', 'hash', 'line_count'])
def test_incremental_refuses_invalid_target_vectors(engine, bad_target):
    before = engine.state()
    target = list(engine.append(observation(native_id='BAD-TARGET')))
    if bad_target == 'omit':
        target = [b for b in target if b.relative_path.startswith('owner/')]
    elif bad_target == 'duplicate':
        target.append(target[-1])
    else:
        b = target[-1]
        target[-1] = SourceBoundary('../escape.jsonl' if bad_target == 'traversal' else b.relative_path,
                                   b.line_number + int(bad_target == 'line_count'), b.end_offset,
                                   '0' * 64 if bad_target == 'hash' else b.prefix_sha256)
    with pytest.raises(RebuildRequired):
        engine.apply(target)
    assert engine.state() == before


def test_incremental_decodes_only_new_tail_records(engine, monkeypatch):
    import yeoman_gateway.history.incremental as incremental

    engine.append(observation(native_id='TAIL'))
    decoded_ids = []
    original = incremental.json.loads

    def decode(*args, **kwargs):
        record = original(*args, **kwargs)
        if isinstance(record, dict) and record.get('kind') == 'message':
            decoded_ids.append(record['native']['payload']['messageId'])
        return record

    monkeypatch.setattr(incremental.json, 'loads', decode)
    engine.apply()
    assert decoded_ids == ['TAIL']
    engine.parity()


def test_incremental_pair_keys_accept_hashes_and_unicode(engine):
    for kind in ('outbound_request', 'outbound_result'):
        engine.append(paired(kind, corr='pair#🐎'))
        engine.apply()
        engine.restart()
    engine.parity()
    assert engine.conn.execute("SELECT count(*) FROM messages WHERE native_message_id='S'").fetchone()[0] == 1


@pytest.mark.perf
def test_incremental_apply_cost_is_independent_of_archive_size(tmp_path):
    from statistics import median
    from time import perf_counter

    results = []
    for size in (3000, 30000):
        out = tmp_path / str(size)
        root, db = out / 'raw', out / 'history.db'
        write_seed(root)
        rows = []
        for number in range(size):
            chat = G if number < size // 3 else f'synthetic-{number % 200}@g.us'
            sender = f'{4915551000000 + number % 1500}@s.whatsapp.net'
            rows.append(observation(native_id=f'M{number}', text=f'unique {number}',
                                    ms=T0 + number * 1000, sender=sender, chat=chat))
        rows.extend(observation('reaction', native_id=f'M{number}', sender=PN,
                                ms=T0 + number * 1000) for number in range(20))
        rows.extend([paired('outbound_request', corr='seed-pair'),
                     paired('outbound_result', corr='seed-pair')])
        write_jsonl(root / 'whatsapp/2026-01.jsonl', rows)
        project([root], db)
        with closing(sqlite3.connect(db)) as conn:
            conn.execute('PRAGMA foreign_keys=ON')
            index = ProjectionIndex.from_prefix(root, enumerate_committed(root))
            samples, targets = [], []
            for number, kind in enumerate(('message', 'reaction', 'receipt', 'pair', 'message', 'reaction', 'pair', 'receipt')):
                if kind == 'pair':
                    additions = [paired('outbound_request', corr=f'cost-{number}'),
                                 paired('outbound_result', corr=f'cost-{number}', native_id=f'C{number}')]
                else:
                    additions = [observation(kind, native_id='M0' if kind == 'reaction' else f'C{number}',
                                             text=f'cost {number}', sender='4915551000000@s.whatsapp.net',
                                             ms=T0 + size * 1000 + number * 1000)]
                for row in additions:
                    append_line(root / 'whatsapp/2026-01.jsonl', canonical_json(row))
                started = perf_counter()
                target = index.target(root) if hasattr(index, 'target') else enumerate_committed(root)
                targets.append(perf_counter() - started)
                started = perf_counter()
                apply_committed(conn, index, root, target)
                samples.append(perf_counter() - started)
            results.append((median(samples), max(targets)))
            print(f'\nsize={size} apply_seconds={samples} median={median(samples):.6f} target_seconds={targets}', flush=True)
            oracle = out / 'oracle.db'
            project([root], oracle)
            assert table_digest(db) == table_digest(oracle)
            with closing(sqlite3.connect(oracle)) as expected:
                assert conn.execute('SELECT * FROM messages_current ORDER BY message_id').fetchall() == expected.execute(
                    'SELECT * FROM messages_current ORDER BY message_id').fetchall()
    small, full = results
    assert full[0] <= small[0] * 3
    assert full[0] <= 0.3
    assert max(small[1], full[1]) <= 0.05


def test_incremental_target_reads_only_tail_and_ignores_partial_line(engine, monkeypatch):
    import yeoman_gateway.history.incremental as incremental

    old = {b.relative_path: b.end_offset for b in engine.boundaries()}
    engine.append(observation(native_id='TARGET'))
    path = engine.root / 'whatsapp/2026-01.jsonl'
    with path.open('ab') as out:
        out.write(b'{"incomplete":')
    reads = []
    original = incremental.os.pread

    def read(fd, size, offset):
        reads.append(offset)
        return original(fd, size, offset)

    def forbidden(*args):
        raise AssertionError('target must not fsync')

    monkeypatch.setattr(incremental.os, 'pread', read)
    monkeypatch.setattr(incremental.os, 'fsync', forbidden)
    target = engine.index.target(engine.root)
    assert reads and min(reads) >= min(old.values())
    boundary = next(b for b in target if b.relative_path.startswith('whatsapp/'))
    assert boundary.end_offset == path.stat().st_size - len(b'{"incomplete":')
    engine.apply(target)
    assert engine.conn.execute("SELECT count(*) FROM messages WHERE native_message_id='TARGET'").fetchone()[0] == 1


def test_incremental_key_closure_keeps_late_join_chain_and_target(engine):
    for ms in (T0, T0 + 100_000, T0 + 200_000):
        engine.append(observation('reaction', native_id='LATE', ms=ms))
        engine.apply()
    assert engine.conn.execute('SELECT DISTINCT target_message_id FROM message_events').fetchall() == [(None,)]
    engine.append(observation(native_id='LATE', text='same'))
    engine.apply()
    engine.parity()
    assert engine.conn.execute('SELECT DISTINCT target_message_id FROM message_events').fetchall() == [(f'whatsapp:{G}:LATE',)]
    engine.append(_bf('session_jsonl', 'message', {'text': 'same', 'senderId': PN}, chat=G),
                  'backfill/session_jsonl.jsonl')
    engine.apply()
    engine.append(observation(native_id='SPLIT', text='same'))
    engine.append(observation('reaction', native_id='LATE', ms=T0 - 50_000))
    engine.apply()
    engine.parity()
    assert engine.conn.execute("SELECT count(*) FROM messages WHERE text='same'").fetchone()[0] == 3


def test_incremental_known_native_pair_and_name_reuse_resolution(engine, monkeypatch):
    import yeoman_gateway.history.incremental as incremental

    def known(native_id, ms):
        row = observation(native_id=native_id, ms=ms)
        row['native']['payload'].update(participantJid='97001@lid', senderPhoneJid=PN, senderName='Known name')
        return row
    engine.append(known('KNOWN', T0 + 1000))
    engine.apply()
    engine.parity()
    def forbidden(*args):
        raise AssertionError('known identity must not run full resolve')
    monkeypatch.setattr(incremental, 'resolve', forbidden)
    engine.append(known('KNOWN-NEW', T0 + 2000))
    engine.apply()
    engine.parity()


def test_incremental_known_numeric_sighting_reuses_resolution(engine, monkeypatch):
    import yeoman_gateway.history.incremental as incremental

    engine.append(observation(native_id='NUM-KNOWN', sender='4915550000001', ms=T0 + 1000))
    engine.apply()
    engine.parity()
    def forbidden(*args):
        raise AssertionError('known numeric applicability must reuse resolution')
    monkeypatch.setattr(incremental, 'resolve', forbidden)
    engine.append(observation('reaction', sender='4915550000001', ms=T0 + 2000))
    engine.apply()
    engine.parity()


def test_incremental_target_discovers_new_files_and_matches_committed_vector(engine):
    engine.append(observation(native_id='MONTH'), 'whatsapp/2026-02.jsonl')
    engine.append({'kind': 'media_description', 'native_message_id': 'MONTH', 'chat_id': G,
                   'text': 'derived'}, 'derived/media-descriptions.jsonl')
    target = engine.index.target(engine.root)
    assert target == enumerate_committed(engine.root)
    engine.apply(target)
    engine.parity()


def test_incremental_receipt_writes_no_semantic_rows(engine):
    sql = []
    engine.conn.set_trace_callback(sql.append)
    engine.append(observation('receipt'))
    engine.apply()
    semantic = ('contacts', 'identifier_history', 'messages', 'message_events')
    assert not [line for line in sql if line.startswith(('INSERT ', 'DELETE ', 'UPDATE '))
                and any(f' {table} ' in line or f' {table}(' in line for table in semantic)]
    engine.parity()


def test_incremental_known_sender_metadata_does_not_write_unrelated_contact(engine):
    engine.append(observation(native_id='OTHER', sender=OTHER))
    engine.apply()
    other_cid = engine.conn.execute("SELECT sender_contact_id FROM messages WHERE native_message_id='OTHER'").fetchone()[0]
    sql = []
    engine.conn.set_trace_callback(sql.append)
    engine.append(observation('reaction', native_id='SEED', ms=T0 + 1000))
    engine.apply()
    assert not [line for line in sql if line.startswith(('INSERT ', 'DELETE ', 'UPDATE '))
                and (' contacts ' in line or ' identifier_history ' in line) and other_cid in line]
    engine.parity()


def test_incremental_previously_missing_author_target_requires_rebuild(tmp_path):
    root, db = tmp_path / 'raw', tmp_path / 'history.db'
    write_seed(root)
    write_jsonl(root / 'whatsapp/2026-01.jsonl', [observation(native_id='SEED')])
    write_jsonl(root / 'owner/corrections.jsonl', [make('author', T0, 'future ref',
                source_ref='whatsapp/2026-01.jsonl#2', anchor=PN)])
    project([root], db)
    with closing(sqlite3.connect(db)) as conn:
        boundary = enumerate_committed(root)
        index = ProjectionIndex.from_prefix(root, boundary)
        before = table_digest(db), conn.execute('SELECT * FROM projector_state ORDER BY file').fetchall()
        append_line(root / 'whatsapp/2026-01.jsonl', canonical_json(observation(native_id='LATE')))
        with pytest.raises(RebuildRequired):
            apply_committed(conn, index, root, index.target(root))
        assert (table_digest(db), conn.execute('SELECT * FROM projector_state ORDER BY file').fetchall()) == before


def test_incremental_reaction_to_named_message_does_not_expand_whole_chat(engine, monkeypatch):
    import yeoman_gateway.history.incremental as incremental

    for number in range(20):
        row = observation(native_id=f'NAMED-{number}', ms=T0 + 1000 + number * 1000)
        row['native']['payload']['senderName'] = 'Known name'
        engine.append(row)
    engine.apply()
    seen = []
    original = incremental._messages
    def normalize(ex, *args):
        seen.extend(c.native_id for c in ex.messages)
        return original(ex, *args)
    monkeypatch.setattr(incremental, '_messages', normalize)
    engine.append(observation('reaction', native_id='NAMED-0', ms=T0 + 50_000))
    engine.apply()
    assert set(seen) <= {'NAMED-0'}
    engine.parity()
