"""Explicit, verified v2 → v3 offline copy; runtime startup never migrates."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Any

from yeoman_shared.raw_archive.paths import is_protected

from yeoman_gateway.knowledge._store import _CORE_SCHEMA

HISTORY_SCHEMA_VERSION = 3
FROZEN_IDENTITY_TABLES = (
    'contacts', 'contact_identifiers', 'contact_aliases', 'contact_fields',
    'knowledge_identifier_bindings', 'knowledge_identity_redirects', 'knowledge_identity_ops',
    'knowledge_identity_candidates', 'knowledge_identity_candidate_evidence',
    'knowledge_provider_pair_evidence', 'knowledge_provider_pair_sources', 'knowledge_provider_stitch_proposals',
)
_HISTORY_DDL = (
    """CREATE TABLE knowledge_history_sources (
        event_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0),
        source_json TEXT NOT NULL CHECK(json_valid(source_json)),
        content_fingerprint TEXT NOT NULL, author_contact_id TEXT NOT NULL,
        audience_json TEXT NOT NULL CHECK(json_valid(audience_json)),
        revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1)), reason TEXT,
        PRIMARY KEY(event_id,revision))""",
    """CREATE TABLE knowledge_history_source_aliases (
        event_id TEXT NOT NULL, revision INTEGER NOT NULL,
        issued_json TEXT NOT NULL CHECK(json_valid(issued_json)), message_id TEXT NOT NULL,
        message_revision INTEGER NOT NULL, author_contact_id TEXT NOT NULL,
        content_fingerprint TEXT NOT NULL, audience_json TEXT NOT NULL CHECK(json_valid(audience_json)),
        PRIMARY KEY(event_id,revision))""",
    """CREATE TABLE knowledge_history_capture (
        message_id TEXT NOT NULL, revision INTEGER NOT NULL, job_id TEXT, outcome TEXT NOT NULL,
        PRIMARY KEY(message_id,revision))""",
    """CREATE TABLE knowledge_history_capture_state (
        key TEXT PRIMARY KEY, value_json TEXT NOT NULL CHECK(json_valid(value_json)),
        version INTEGER NOT NULL)""",
)


def _digest(db: sqlite3.Connection, tables: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for name in tables:
        rows = sorted((tuple(row) for row in db.execute(f'SELECT * FROM "{name}"')), key=repr)
        if name == 'knowledge_meta':
            rows = [row for row in rows if row[0] != 'schema_version']
        digest.update(repr((name, rows)).encode())
    return digest.hexdigest()


def _isolated(path: Path) -> None:
    if is_protected(path) or Path('/home/dm/.yeoman/data') in path.parents:
        raise ValueError('isolated_snapshot_required')


def upgrade_history_knowledge(*, source: Path, target: Path) -> dict[str, Any]:
    source, target = source.expanduser().resolve(), target.expanduser().resolve()
    _isolated(source)
    _isolated(target)
    if not source.is_file() or target.exists() or source == target:
        raise ValueError('new_isolated_target_required')
    if any(Path(str(target) + suffix).exists() for suffix in ('-wal', '-shm', '-journal')):
        raise ValueError('target_sidecar_exists')
    # An offline immutable snapshot must be checkpointed, otherwise WAL would be lost.
    if Path(str(source) + '-wal').exists() and Path(str(source) + '-wal').stat().st_size:
        raise ValueError('checkpointed_offline_snapshot_required')
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro&immutable=1', uri=True)) as original:
        version = original.execute("SELECT value FROM knowledge_meta WHERE key='schema_version'").fetchone()
        complete = original.execute("SELECT value FROM knowledge_meta WHERE key='migration_complete'").fetchone()
        if version != ('2',) or complete != ('1',):
            raise ValueError('verified_schema_2_required')
        if original.execute('PRAGMA integrity_check').fetchone() != ('ok',):
            raise ValueError('source_integrity_failed')
        tables = tuple(row[0] for row in original.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"))
        before = _digest(original, tables)
        fd, temporary = tempfile.mkstemp(prefix=target.name + '.', suffix='.copy', dir=target.parent)
        os.close(fd)
        staged = Path(temporary)
        try:
            with closing(sqlite3.connect(staged)) as copied:
                original.backup(copied)
                copied.execute('PRAGMA journal_mode=DELETE')
                copied.execute('PRAGMA foreign_keys=OFF')
                copied.execute('BEGIN')
                for table in ('knowledge_statement_people', 'knowledge_person_attributes'):
                    indexes = [row[0] for row in copied.execute(
                        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (table,))]
                    ddl = next(sql for sql in _CORE_SCHEMA if f'CREATE TABLE IF NOT EXISTS {table} (' in sql)
                    ddl = ddl.replace(f'CREATE TABLE IF NOT EXISTS {table}', f'CREATE TABLE {table}_copy')
                    ddl = ddl.replace('person_id TEXT NOT NULL REFERENCES contacts(id)', 'person_id TEXT NOT NULL')
                    copied.execute(ddl)
                    copied.execute(f'INSERT INTO {table}_copy SELECT * FROM {table}')
                    copied.execute(f'DROP TABLE {table}')
                    copied.execute(f'ALTER TABLE {table}_copy RENAME TO {table}')
                    for sql in indexes:
                        copied.execute(sql)
                for sql in _HISTORY_DDL:
                    copied.execute(sql)
                for table in FROZEN_IDENTITY_TABLES:
                    for operation in ('INSERT', 'UPDATE', 'DELETE'):
                        copied.execute(f"CREATE TRIGGER history_frozen_{table}_{operation.lower()} BEFORE {operation} ON {table}"
                                       " BEGIN SELECT RAISE(ABORT, 'history_identity_read_only'); END")
                copied.execute("UPDATE knowledge_meta SET value='3' WHERE key='schema_version'")
                after = _digest(copied, tables)
                if before != after or copied.execute('PRAGMA foreign_key_check').fetchall():
                    raise ValueError('copy_verification_failed')
                if copied.execute('PRAGMA integrity_check').fetchone() != ('ok',):
                    raise ValueError('target_integrity_failed')
                copied.commit()
            with staged.open('rb') as handle:
                os.fsync(handle.fileno())
            # No overwrite, even if another publisher created the target after preflight.
            os.link(staged, target)
            directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            staged.unlink(missing_ok=True)
    return {'schema_version': 3, 'source_digest': before, 'target_digest': after,
            'published': True, 'tables_verified': len(tables)}


def inventory_history_sources(*, source: Path) -> dict[str, int]:
    """Counts only, read-only coordinator entrypoint on a checkpointed isolated copy."""
    source = source.expanduser().resolve()
    _isolated(source)
    if Path(str(source) + '-wal').exists() and Path(str(source) + '-wal').stat().st_size:
        raise ValueError('checkpointed_offline_snapshot_required')
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        counts = {'statement_refs': 0, 'queued_refs': 0, 'revoked': 0,
                  'native': 0, 'event': 0, 'revisions': 0, 'mapped': 0, 'ambiguous': 0, 'missing': 0}
        keys = set()
        for event, revision, status in db.execute('SELECT event_id,revision,status FROM knowledge_statement_sources'):
            counts['statement_refs'] += 1
            counts['revoked'] += status == 'revoked'
            keys.add((event, revision))
        for (payload,) in db.execute("SELECT sources_json FROM knowledge_jobs WHERE state IN ('queued','running')"):
            refs = json.loads(payload)
            counts['queued_refs'] += len(refs)
            keys.update((ref['event_id'], ref['revision']) for ref in refs)
        counts['event'] = len({event for event, _ in keys})
        counts['revisions'] = len(keys)
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='knowledge_history_source_aliases'").fetchone()
        mapped = set(db.execute('SELECT event_id,revision FROM knowledge_history_source_aliases')) if exists else set()
        counts['mapped'] = len(keys & mapped)
        counts['missing'] = len(keys - mapped)
        return counts
