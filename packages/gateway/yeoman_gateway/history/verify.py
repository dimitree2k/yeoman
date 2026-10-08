"""Verify a built history.db: coverage, review list, accounting and rebuild determinism."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path
from typing import Any

from yeoman_shared.raw_archive.records import SourceBoundary

from .convert.common import open_ro
from .extract import extract
from .layer1 import canonical_json, iter_layer1, layer1_files
from .project import build_rows, project
from .schema import SCHEMA_VERSION

TABLES = ("contacts", "identifier_history", "messages", "message_events")


def _open(db_path: Path, *, frozen: bool = False) -> sqlite3.Connection:
    conn = (open_ro(db_path) if frozen else
            sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True))
    conn.execute("BEGIN")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        conn.close()
        from .incremental import RebuildRequired

        raise RebuildRequired(f"history schema {version} requires rebuild; no in-place migration")
    return conn


def table_digest(db_path: Path, *, frozen: bool = False) -> dict[str, str]:
    with closing(_open(db_path, frozen=frozen)) as conn:
        return _table_digest(conn)


def _table_digest(conn: sqlite3.Connection) -> dict[str, str]:
    digests: dict[str, str] = {}
    for table in TABLES:
        cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")
                if not (table == "identifier_history" and row[1] == "id")]
        digest = hashlib.sha256()
        for row in conn.execute(f"SELECT {', '.join(cols)} FROM {table} ORDER BY {', '.join(cols)}"):
            digest.update(canonical_json(list(row)).encode("utf-8") + b"\n")
        digests[table] = digest.hexdigest()
    return digests


def coverage(db_path: Path, *, frozen: bool = False) -> dict[str, Any]:
    with closing(_open(db_path, frozen=frozen)) as conn:
        return _coverage(conn)


def _coverage(conn: sqlite3.Connection) -> dict[str, Any]:
    total, with_contact, confirmed = conn.execute(
        "SELECT count(*), count(m.sender_contact_id), coalesce(sum(c.status = 'confirmed'), 0)"
        " FROM messages m LEFT JOIN contacts c ON c.contact_id = m.sender_contact_id"
        " WHERE m.sender_identifier IS NOT NULL").fetchone()
    provisional = [
        {"contact_id": row[0], "display_name": row[1], "messages": row[2],
         "identifiers": json.loads(row[3])}
        for row in conn.execute(
            "SELECT c.contact_id, c.display_name,"
            " (SELECT count(*) FROM messages m WHERE m.sender_contact_id = c.contact_id),"
            " (SELECT json_group_array(value) FROM (SELECT i.value FROM identifier_history i"
            "   WHERE i.contact_id = c.contact_id AND i.kind != 'push_name' ORDER BY i.value))"
            " FROM contacts c WHERE c.status = 'provisional' AND c.merged_into IS NULL"
            " ORDER BY 3 DESC, c.contact_id")]
    unresolved = [{"message_id": row[0], "sender_identifier": row[1]} for row in conn.execute(
        "SELECT message_id, sender_identifier FROM messages"
        " WHERE sender_identifier IS NOT NULL AND sender_contact_id IS NULL"
        " ORDER BY message_id LIMIT 100")]
    basis = dict(conn.execute("SELECT sender_basis, count(*) FROM messages GROUP BY 1 ORDER BY 1"))
    provenance = dict(conn.execute("SELECT provenance, count(*) FROM messages GROUP BY 1 ORDER BY 1"))
    ratio = confirmed / total if total else 1.0
    return {"messages_with_identifier": total, "with_contact": with_contact,
            "with_confirmed_contact": confirmed, "confirmed_ratio": round(ratio, 4),
            "meets_95_percent": ratio >= 0.95, "provisional_contacts": provisional,
            "unresolved_messages": unresolved, "sender_basis": basis, "provenance": provenance}


def _input_pin(roots: Sequence[Path]) -> dict[str, str]:
    return {rel: hashlib.sha256(path.read_bytes()).hexdigest()
            for rel, path in layer1_files(roots)}


def verify(roots: Sequence[Path], db_path: Path, *, scratch: Path | None,
           frozen: bool = False) -> dict[str, Any]:
    if scratch is not None and not frozen:
        raise ValueError("determinism requires explicitly frozen inputs")
    pinned = _input_pin(roots) if frozen else None
    with closing(_open(db_path, frozen=frozen)) as conn:
        report: dict[str, Any] = {"coverage": _coverage(conn), "digests": _table_digest(conn)}
    report["boundary"] = {"mode": "frozen" if frozen else "live",
                          "database": "single_read_transaction", "tail_freshness": False}
    if pinned is not None:
        report["input_digests"] = pinned
    ex = extract(iter_layer1(roots))
    resolution = build_rows(ex).resolution
    report["review"] = resolution.review
    accounted: Counter[str] = Counter()
    for (file, _), count in ex.outcomes.items():
        accounted[file] += count
    lines = {}
    blanks = {}
    for rel, path in layer1_files(roots):
        data = path.read_bytes()
        if data and not data.endswith(b"\n"):
            raise ValueError(f"incomplete Layer 1 tail: {rel}")
        physical = data.split(b"\n")[:-1]
        lines[rel] = len(physical)
        blanks[rel] = sum(not line.decode("utf-8", errors="replace").strip() for line in physical)
        accounted[rel] += blanks[rel]
    report["blank_lines_skipped"] = blanks
    report["accounting"] = {rel: {"lines": count, "accounted": accounted.get(rel, 0)}
                            for rel, count in lines.items()}
    report["accounting_ok"] = all(item["lines"] == item["accounted"]
                                   for item in report["accounting"].values())

    def check_inputs() -> None:
        if pinned is not None and _input_pin(roots) != pinned:
            raise ValueError("frozen Layer 1 inputs changed during verification")

    check_inputs()
    if scratch is not None:
        if any(scratch.resolve().is_relative_to(root.resolve()) or
               root.resolve().is_relative_to(scratch.resolve()) for root in roots):
            raise ValueError("scratch must be separate from frozen inputs")
        first, second = scratch / "rebuild-a.db", scratch / "rebuild-b.db"
        if db_path.resolve() in (first.resolve(), second.resolve()):
            raise ValueError("scratch rebuild must not replace the original database")
        scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
        project(roots, first)
        check_inputs()
        project(roots, second)
        check_inputs()
        report["deterministic"] = (table_digest(first, frozen=True) ==
                                   table_digest(second, frozen=True) == report["digests"])
    return report


def _expected_semantic_rows(rows: Any) -> dict[str, list[tuple[Any, ...]]]:
    # Match the stored column surface, including the distinct JSON encodings in write_rows.
    return {
        'contacts': [(c.contact_id, c.kind, c.role, c.display_name, c.status, c.merged_into,
                      json.dumps(list(c.source_refs))) for c in rows.resolution.contacts],
        'identifier_history': [(i.contact_id, i.channel, i.kind, i.value, i.strength, i.evidence,
                                i.first_seen_ms, i.last_seen_ms, i.ended_ms, i.valid_from_ms,
                                i.valid_until_ms, json.dumps(list(i.source_refs)))
                               for i in rows.resolution.identifiers],
        'messages': [(m['message_id'], m['channel'], m['chat_id'], m['native_message_id'],
                      m['sender_contact_id'], m['sender_identifier'], m['sender_basis'], m['direction'],
                      m['sent_ms'], m['time_certainty'], m['text'],
                      canonical_json(m['media']) if m['media'] else None, m['reply_to_native_id'],
                      canonical_json(m['mentions']) if m['mentions'] else None,
                      m['provenance'], json.dumps(m['source_refs'])) for m in rows.messages],
        'message_events': [(e['event_id'], e['kind'], e['channel'], e['chat_id'], e['target_message_id'],
                            e['target_native_id'], e['actor_contact_id'], e['actor_identifier'],
                            e['actor_basis'], e['occurred_ms'], e['time_certainty'],
                            canonical_json(e['payload']), e['provenance'], json.dumps(e['source_refs']),
                            e['native_event_id']) for e in rows.events],
    }


def verify_rebuild_candidate(roots: Sequence[Path], db_path: Path, *,
                             boundaries: Sequence[SourceBoundary]) -> dict[str, Any]:
    from .schema import PROJECTOR_VERSION, create

    def pin() -> dict[str, tuple[int, int, str]]:
        result = {}
        for rel, path in layer1_files(roots):
            if rel in result:
                raise ValueError('prefix_inventory_mismatch')
            data = path.read_bytes()
            if data and not data.endswith(b'\n'):
                raise ValueError('prefix_incomplete')
            result[rel] = (data.count(b'\n'), len(data), hashlib.sha256(data).hexdigest())
        return result

    expected_pin = {b.relative_path: (b.line_number, b.end_offset, b.prefix_sha256) for b in boundaries}
    if len(expected_pin) != len(boundaries) or pin() != expected_pin:
        raise ValueError('prefix_inventory_mismatch')
    ex = extract(iter_layer1(roots))
    rows = build_rows(ex)
    reserved = {line.record['contact_id'] for line in ex.contact_id_records if line.record}
    if any(item.contact_id not in reserved for item in rows.resolution.generated_ids):
        raise ValueError('lineage_incomplete')
    contact_ids = {c.contact_id for c in rows.resolution.contacts}
    for contact in rows.resolution.contacts:
        if rows.resolution.terminal(contact.contact_id) not in contact_ids:
            raise ValueError('redirect_target_missing')
        rows.resolution.terminal(contact.contact_id)  # Reject cycles/missing redirects before publication.
    expected_outcomes = {rel: dict(values) for rel, values in rows.report['outcomes'].items()}
    accounted: Counter[str] = Counter()
    for (rel, _), count in ex.outcomes.items():
        accounted[rel] += count
    for rel, path in layer1_files(roots):
        blanks = sum(not line.decode('utf-8', errors='replace').strip()
                     for line in path.read_bytes().split(b'\n')[:-1])
        accounted[rel] += blanks
        if blanks:
            expected_outcomes.setdefault(rel, {})['skipped:blank'] = blanks
    if any(accounted[rel] != vector[0] for rel, vector in expected_pin.items()):
        raise ValueError('accounting_mismatch')
    semantic = _expected_semantic_rows(rows)
    expected = {}
    for table, tuples in semantic.items():
        # SQLite sorts NULL before numeric before text. These columns have fixed types.
        ordered = sorted(tuples, key=lambda row: tuple((0, '') if v is None else
                         (1, v) if isinstance(v, (int, float)) else (2, v) for v in row))
        digest = hashlib.sha256()
        for row in ordered:
            digest.update(canonical_json(list(row)).encode('utf-8') + b'\n')
        expected[table] = digest.hexdigest()
    with closing(_open(db_path)) as conn, closing(sqlite3.connect(':memory:')) as surface:
        create(surface)
        for table in (*TABLES, 'projector_state'):
            if conn.execute(f'PRAGMA table_info({table})').fetchall() != surface.execute(f'PRAGMA table_info({table})').fetchall():
                raise ValueError('schema_surface_mismatch')
        if conn.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('integrity_mismatch')
        if conn.execute('PRAGMA foreign_key_check').fetchall() != []:
            raise ValueError('foreign_key_mismatch')
        states = conn.execute('SELECT file, lines, end_offset, sha256, projector_version, state_json FROM projector_state').fetchall()
        actual_pin = {s[0]: tuple(s[1:4]) for s in states if s[0] != '@runtime'}
        if actual_pin != expected_pin or any(s[4] != PROJECTOR_VERSION for s in states):
            raise ValueError('checkpoint_mismatch')
        runtime = [s for s in states if s[0] == '@runtime']
        if len(runtime) != 1 or runtime[0][1:4] != (0, 0, hashlib.sha256(b'').hexdigest()):
            raise ValueError('runtime_mismatch')
        state = json.loads(runtime[0][5])
        if (not isinstance(state, dict) or type(state.get('generation')) is not int or state['generation'] < 1 or
                state.get('status') not in ('ready', 'rebuilding', 'failed') or
                state.get('admission', state.get('status')) not in ('ready', 'rebuilding', 'failed') or
                state.get('pending_pairs') != ex.pending_pairs or
                state.get('outcomes') != expected_outcomes or
                state.get('review') != {key: len(items) for key, items in rows.report['review'].items()}):
            raise ValueError('runtime_mismatch')
        candidate = _table_digest(conn)
        if candidate != expected:
            raise ValueError('semantic_digest_mismatch')
    if pin() != expected_pin:
        raise ValueError('prefix_changed')
    return {'verified': True, 'expected_digests': expected, 'candidate_digests': candidate,
            'checkpoints_match': True, 'integrity_ok': True, 'foreign_keys_ok': True, 'accounting_ok': True}
