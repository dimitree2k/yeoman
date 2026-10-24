"""Read-only, content-free inventory of Knowledge's directory bridge."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

_COUNTS = ('mapped', 'unmapped', 'ambiguous')
_REFERENCES = """
WITH refs(source, role, person_id, statement_id) AS (
 SELECT 'speaker', '', speaker_person_id, statement_id
 FROM knowledge_statements WHERE status = 'assertion'
 UNION ALL
 SELECT 'roles', role, person_id, statement_id FROM knowledge_statement_people
 UNION ALL
 SELECT 'attributes', '', person_id, statement_id FROM knowledge_person_attributes
)
SELECT source, role, person_id, json_group_array(DISTINCT statement_id)
FROM refs WHERE person_id IS NOT NULL AND trim(person_id) <> ''
GROUP BY source, role, person_id
"""


@contextmanager
def _read_database(path: Path) -> Iterator[sqlite3.Connection]:
    path = path.expanduser().resolve()
    wal = path.with_name(path.name + '-wal')
    before = path.stat()
    # A checkpointed WAL database otherwise creates sidecars even with mode=ro.
    checkpointed = not wal.exists()
    uri = f'{path.as_uri()}?mode=ro' + ('&immutable=1' if checkpointed else '')
    conn = sqlite3.connect(uri, uri=True)
    try:
        yield conn
        after = path.stat()
        if checkpointed and (wal.exists() or
                             (before.st_mtime_ns, before.st_size, before.st_ino) !=
                             (after.st_mtime_ns, after.st_size, after.st_ino)):
            raise ValueError('Database changed during checkpointed read; retry.')
    finally:
        conn.close()


def knowledge_mapping(knowledge_db: Path, people_db: Path) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Count ids per source, and distinct active statements per mapping outcome.

    Role and attribute inventories include historical rows; statement counts include
    only assertions. Categories can overlap when a statement references several ids.
    """
    with ExitStack() as stack:
        knowledge = stack.enter_context(_read_database(knowledge_db))
        people = stack.enter_context(_read_database(people_db))
        for conn in (knowledge, people):
            conn.execute('PRAGMA query_only=ON')
            conn.execute('BEGIN')
        refs = knowledge.execute(_REFERENCES).fetchall()
        active = dict(knowledge.execute(
            "SELECT statement_id, speaker_person_id FROM knowledge_statements "
            "WHERE status='assertion'"
        ))
        parents = dict(people.execute('SELECT id, merged_into FROM persons'))
        bridges: dict[str, set[int]] = {}
        invalid: set[str] = set()
        for kid, person in people.execute(
            "SELECT value, person_id FROM person_ids WHERE kind='knowledge_person' "
            'AND valid_until_ms IS NULL AND hidden=0'
        ):
            seen: set[int] = set()
            while person in parents and parents[person] is not None and person not in seen:
                seen.add(person)
                person = parents[person]
            if person not in parents or person in seen:
                invalid.add(kid)
            else:
                bridges.setdefault(kid, set()).add(person)

    def counts() -> dict[str, int]:
        return dict.fromkeys(_COUNTS, 0)

    report: dict[str, Any] = {
        'persons': counts(),
        'sources': {'speaker': counts(), 'roles': {}, 'attributes': counts()},
        'statements': counts(),
        'reasons': {'no_active_bridge': 0, 'multiple_terminals': 0, 'invalid_merge_chain': 0},
    }
    outcomes: dict[str, str] = {}
    details = []
    statements: dict[str, set[str]] = {key: set() for key in _COUNTS}
    for source, role, kid, statement_ids in refs:
        if kid not in outcomes:
            terminals = bridges.get(kid, set())
            reason = None
            if kid in invalid:
                outcome, reason = 'unmapped', 'invalid_merge_chain'
            elif not terminals:
                outcome, reason = 'unmapped', 'no_active_bridge'
            elif len(terminals) > 1:
                outcome, reason = 'ambiguous', 'multiple_terminals'
            else:
                outcome = 'mapped'
            outcomes[kid] = outcome
            report['persons'][outcome] += 1
            if reason:
                report['reasons'][reason] += 1
                details.append({'knowledge_id': kid, 'reason': reason})
        outcome = outcomes[kid]
        target = report['sources'][source]
        if source == 'roles':
            target = target.setdefault(role, counts())
        target[outcome] += 1
        statements[outcome].update(sid for sid in json.loads(statement_ids) if sid in active)
    report['statements'] = {key: len(value) for key, value in statements.items()}
    report['statements']['no_speaker'] = sum(
        not speaker or not speaker.strip() for speaker in active.values()
    )
    return report, sorted(details, key=lambda row: row['knowledge_id'])


def write_mapping_details(
    path: Path, details: list[dict[str, str]], *, protected: tuple[Path, ...] = (),
) -> None:
    """Atomically replace an owner-only file outside any git work tree."""
    requested = path.expanduser().absolute()
    path = requested.resolve()
    for candidate in (requested, path):
        for parent in (candidate.parent, *candidate.parent.parents):
            marker = parent / '.git'
            if marker.is_file() or marker.is_symlink() or (marker / 'HEAD').is_file():
                raise ValueError('Details path must be outside a git work tree.')
    if any(path == source.expanduser().resolve() or
           (path.exists() and path.samefile(source.expanduser())) for source in protected):
        raise ValueError('Details must not replace an input database.')
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = path.parent.stat()
    if parent.st_uid != os.geteuid() or parent.st_mode & 0o777 != 0o700:
        raise ValueError('Details parent must be owner-only (0700).')
    fd, temporary = tempfile.mkstemp(prefix='.knowledge-mapping-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(details, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
