"""Synthetic v2 copy fixtures shared by the Tasks 4–5 checks."""
import hashlib
import sqlite3

import pytest
from yeoman_gateway.knowledge._store import KnowledgeStore
from yeoman_gateway.knowledge.api import KnowledgeStartupError, open_knowledge_store
from yeoman_gateway.knowledge.runtime import RuntimeKnowledgePolicy, RuntimeKnowledgeSources


def digest(db, table):
    rows = sorted((tuple(row) for row in db.execute(f'SELECT * FROM "{table}"')), key=repr)
    return hashlib.sha256(repr(rows).encode()).hexdigest()


def v2(path):
    store = KnowledgeStore(path)
    store.set_meta("migration_complete", "1")
    store.execute("INSERT INTO contacts(id,display_name,created_at,updated_at) VALUES ('curated','Synthetic','date','date')")
    store.execute("INSERT INTO knowledge_identifier_bindings(binding_id,channel,kind,namespace,value,person_id,"
                  "valid_from_ms,evidence_ref,mapping_verified,created_ms,updated_ms)"
                  " VALUES ('curated-binding','whatsapp','phone_jid','whatsapp','10009@s.whatsapp.net','curated',1,'synthetic-proof',1,1,1)")
    store.execute("INSERT INTO contact_aliases(contact_id,alias,source,first_seen,last_seen,status,address_allowed,normalized_alias)"
                  " VALUES ('curated','Synthetic','owner_confirmed','date','date','confirmed',1,'synthetic')")
    store.commit_if_idle()
    from yeoman_gateway.knowledge.api import KnowledgeService
    from yeoman_gateway.knowledge.authority import EvidenceAudience
    from yeoman_gateway.knowledge.models import (
        AttributeCandidate,
        AttributeValue,
        PersonLinkCandidate,
        SourceRef,
        StatementCandidate,
        TrustedCaptureContext,
    )
    authority = RuntimeKnowledgeSources()
    source = SourceRef('curated-source', 4, 'whatsapp', 'curated@g.us', 'whatsapp:10009', 100)
    authority.register_source(source, EvidenceAudience.author_only())
    service = KnowledgeService(store=store, workspace_id='synthetic', source_authority=authority,
                               policy_authority=RuntimeKnowledgePolicy(engine=None))
    context = TrustedCaptureContext('synthetic', 1, 'native', (source,))
    result = service.capture(StatementCandidate('Curated synthetic source', (source,), people=(
        PersonLinkCandidate('curated', 'subject', source, 'confirmed'),), attributes=(
        AttributeCandidate('curated', 'description', AttributeValue('Synthetic attribute')),)), context=context)
    service.correct_statement(result.statement_ids[0], StatementCandidate('Curated synthetic correction', (source,)),
                              expected_source=source, context=context)
    service.enqueue_capture((source,), context=context)
    # A persisted suppression/curation control remains opaque to the copy tool.
    store.set_meta('curated_suppression', 'synthetic-withheld-ref')
    service.close()
    return path


def open_service(path, *, history_mode=False):
    return open_knowledge_store(path, workspace_id="synthetic", source_authority=RuntimeKnowledgeSources(),
                                policy_authority=RuntimeKnowledgePolicy(engine=None), history_mode=history_mode)


def test_history_knowledge_upgrade_preserves_curated_rows_and_frozen_identity(tmp_path):
    from yeoman_gateway.knowledge._history_upgrade import upgrade_history_knowledge
    source = v2(tmp_path / "v2.db")
    original = source.read_bytes()
    target = tmp_path / "v3.db"
    with sqlite3.connect(source) as db:
        before = {name: digest(db, name) for (name,) in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ) if name != 'knowledge_meta'}
    receipt = upgrade_history_knowledge(source=source, target=target)
    for name in ('knowledge_statements', 'knowledge_statement_sources', 'knowledge_person_attributes',
                 'knowledge_statement_people', 'knowledge_statement_audit', 'knowledge_jobs'):
        with sqlite3.connect(source) as db:
            assert db.execute(f'SELECT count(*) FROM {name}').fetchone()[0] > 0
    assert source.read_bytes() == original
    assert receipt["schema_version"] == 3 and receipt["published"] is True
    assert receipt["source_digest"] == receipt["target_digest"]
    with sqlite3.connect(target) as db:
        assert {name: digest(db, name) for name in before} == before
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        for table in ('knowledge_statement_people', 'knowledge_person_attributes'):
            fks = db.execute(f'PRAGMA foreign_key_list({table})').fetchall()
            assert all(row[2] != 'contacts' for row in fks)
            assert any(row[2] == 'knowledge_statements' for row in fks)
        assert {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'knowledge_history_%' AND type='table'")} == {
            'knowledge_history_source_aliases', 'knowledge_history_sources',
            'knowledge_history_capture', 'knowledge_history_capture_state'}
    service = open_service(target, history_mode=True)
    service.close()


def test_upgrade_refuses_existing_target_and_normal_start_does_not_migrate(tmp_path):
    from yeoman_gateway.knowledge._history_upgrade import upgrade_history_knowledge
    source = v2(tmp_path / 'v2.db')
    target = tmp_path / 'occupied.db'
    target.write_bytes(b'occupied')
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    with pytest.raises(ValueError):
        upgrade_history_knowledge(source=source, target=target)
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    with pytest.raises(KnowledgeStartupError):
        open_service(source, history_mode=True)
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    service = open_service(source)
    assert service._store.schema_version == 2
    assert not any(name.startswith('knowledge_history_') for name in service._store.table_names())
    service.close()
    with pytest.raises(KnowledgeStartupError):
        open_service(tmp_path / 'fresh-v3.db', history_mode=True)
    assert not (tmp_path / 'fresh-v3.db').exists()


def test_history_upgrade_cli_and_readonly_aggregate_inventory(tmp_path):
    from typer.testing import CliRunner
    from yeoman_gateway.cli.knowledge_commands import app
    from yeoman_gateway.knowledge._history_upgrade import inventory_history_sources
    source = v2(tmp_path / 'source.db')
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    counts = inventory_history_sources(source=source)
    assert counts['statement_refs'] == 2 and counts['queued_refs'] == 1
    assert counts['revisions'] == 1 and counts['missing'] == 1
    assert all(type(value) is int for value in counts.values())
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    target = tmp_path / 'cli-copy.db'
    result = CliRunner().invoke(app, ['knowledge', 'migration', 'upgrade-history', '--source', str(source), '--target', str(target)])
    assert result.exit_code == 0, result.output
    assert target.exists()
