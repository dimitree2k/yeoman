"""people.db schema: owner-editable person directory with audit triggers (spec 2026-10-11 §3)."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from yeoman_shared.utils.helpers import get_operational_data_path

SCHEMA_VERSION = 1

_NOW_MS = "(CAST(strftime('%s', 'now') AS INTEGER) * 1000)"

_DDL = f"""
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS writer_context (id INTEGER PRIMARY KEY CHECK (id = 1), name TEXT);
INSERT OR IGNORE INTO writer_context(id, name) VALUES (1, NULL);

CREATE TABLE IF NOT EXISTS persons (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  preferred_name     TEXT,
  home_city          TEXT,
  address            TEXT,
  birthday           TEXT CHECK (birthday IS NULL
                       OR birthday GLOB '[0-9][0-9][0-9][0-9]-[01][0-9]-[0-3][0-9]'
                       OR birthday GLOB '--[01][0-9]-[0-3][0-9]'),
  relation           TEXT,
  preferred_language TEXT,
  notes              TEXT,
  merged_into        INTEGER REFERENCES persons(id) CHECK (merged_into IS NULL OR merged_into <> id),
  merged_by          TEXT CHECK (merged_by IS NULL OR merged_by IN ('sync', 'owner')),
  is_owner           INTEGER NOT NULL DEFAULT 0 CHECK (is_owner IN (0, 1)),
  level              TEXT NOT NULL DEFAULT 'seen' CHECK (level IN ('contact', 'seen')),
  created_ms         INTEGER NOT NULL DEFAULT {_NOW_MS},
  last_seen_ms       INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS persons_one_owner ON persons(is_owner) WHERE is_owner = 1;
CREATE INDEX IF NOT EXISTS persons_merged ON persons(merged_into);

CREATE TABLE IF NOT EXISTS person_ids (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  person_id      INTEGER NOT NULL REFERENCES persons(id),
  kind           TEXT NOT NULL CHECK (kind IN ('phone', 'wa_lid', 'telegram_id', 'telegram_username',
                   'email', 'wechat', 'history_contact', 'knowledge_person')),
  value          TEXT NOT NULL CHECK (length(trim(value)) > 0),
  source         TEXT NOT NULL DEFAULT 'owner' CHECK (source IN ('observed', 'owner')),
  valid_from_ms  INTEGER,
  valid_until_ms INTEGER,
  hidden         INTEGER NOT NULL DEFAULT 0 CHECK (hidden IN (0, 1)),
  first_seen_ms  INTEGER,
  last_seen_ms   INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS person_ids_active ON person_ids(kind, value)
  WHERE valid_until_ms IS NULL AND hidden = 0;
CREATE INDEX IF NOT EXISTS person_ids_person ON person_ids(person_id);
CREATE INDEX IF NOT EXISTS person_ids_lookup ON person_ids(kind, value);

CREATE TABLE IF NOT EXISTS person_names (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  person_id     INTEGER NOT NULL REFERENCES persons(id),
  name          TEXT NOT NULL CHECK (length(trim(name)) > 0),
  kind          TEXT NOT NULL DEFAULT 'nickname' CHECK (kind IN
                  ('nickname', 'push_name', 'telegram_name', 'contact_name', 'addressed_as')),
  source        TEXT NOT NULL DEFAULT 'owner' CHECK (source IN ('observed', 'owner')),
  hidden        INTEGER NOT NULL DEFAULT 0 CHECK (hidden IN (0, 1)),
  evidence      TEXT NOT NULL DEFAULT '{{}}' CHECK (json_valid(evidence)),
  first_seen_ms INTEGER,
  last_seen_ms  INTEGER,
  UNIQUE (person_id, name, kind, source)
);
CREATE INDEX IF NOT EXISTS person_names_person ON person_names(person_id);

CREATE TABLE IF NOT EXISTS person_decisions (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  person_a   INTEGER NOT NULL REFERENCES persons(id),
  person_b   INTEGER NOT NULL REFERENCES persons(id),
  decision   TEXT NOT NULL CHECK (decision IN ('same', 'not_same')),
  note       TEXT,
  decided_ms INTEGER NOT NULL DEFAULT {_NOW_MS},
  CHECK (person_a <> person_b)
);

CREATE TABLE IF NOT EXISTS suggestions (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  kind       TEXT NOT NULL CHECK (kind IN ('merge', 'detail', 'name')),
  person_a   INTEGER NOT NULL REFERENCES persons(id),
  person_b   INTEGER REFERENCES persons(id),
  field      TEXT,
  value      TEXT,
  evidence   TEXT NOT NULL DEFAULT '{{}}' CHECK (json_valid(evidence)),
  status     TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'accepted', 'rejected')),
  created_ms INTEGER NOT NULL DEFAULT {_NOW_MS},
  decided_ms INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS suggestions_once ON suggestions(
  kind, person_a, coalesce(person_b, 0), coalesce(field, ''), coalesce(value, ''));

CREATE TABLE IF NOT EXISTS change_log (
  id     INTEGER PRIMARY KEY AUTOINCREMENT,
  at_ms  INTEGER NOT NULL DEFAULT {_NOW_MS},
  writer TEXT NOT NULL,
  tbl    TEXT NOT NULL,
  row_id INTEGER NOT NULL,
  op     TEXT NOT NULL CHECK (op IN ('insert', 'update', 'delete')),
  old    TEXT,
  new    TEXT
);

CREATE TRIGGER IF NOT EXISTS persons_no_delete BEFORE DELETE ON persons
BEGIN SELECT RAISE(ABORT, 'persons are never deleted; set merged_into instead'); END;
CREATE TRIGGER IF NOT EXISTS person_ids_no_observed_delete BEFORE DELETE ON person_ids
  WHEN OLD.source = 'observed'
BEGIN SELECT RAISE(ABORT, 'observed rows are not deleted; set hidden = 1'); END;
CREATE TRIGGER IF NOT EXISTS person_names_no_observed_delete BEFORE DELETE ON person_names
  WHEN OLD.source = 'observed'
BEGIN SELECT RAISE(ABORT, 'observed rows are not deleted; set hidden = 1'); END;

CREATE VIEW IF NOT EXISTS persons_v AS
SELECT p.*,
  CASE WHEN coalesce(p.preferred_name, p.home_city, p.address, p.birthday, p.relation,
                     p.preferred_language, p.notes) IS NOT NULL
         OR p.merged_by = 'owner'
         OR EXISTS (SELECT 1 FROM person_names n WHERE n.person_id = p.id AND n.source = 'owner')
         OR EXISTS (SELECT 1 FROM person_ids i WHERE i.person_id = p.id AND i.source = 'owner')
         OR EXISTS (SELECT 1 FROM person_decisions d WHERE p.id IN (d.person_a, d.person_b))
       THEN 'curated' ELSE p.level END AS effective_level,
  (SELECT group_concat(n.name, ', ') FROM person_names n
     WHERE n.person_id = p.id AND n.hidden = 0) AS names,
  (SELECT group_concat(i.kind || ':' || i.value, ', ') FROM person_ids i
     WHERE i.person_id = p.id AND i.hidden = 0 AND i.valid_until_ms IS NULL
       AND i.kind NOT IN ('history_contact', 'knowledge_person')) AS ids
FROM persons p;
"""

# One column map generates every audit trigger, so the log cannot drift from the tables.
_AUDITED: dict[str, tuple[str, ...]] = {
    "persons": ("id", "preferred_name", "home_city", "address", "birthday", "relation",
                "preferred_language", "notes", "merged_into", "merged_by", "is_owner", "level",
                "created_ms", "last_seen_ms"),
    "person_ids": ("id", "person_id", "kind", "value", "source", "valid_from_ms", "valid_until_ms",
                   "hidden", "first_seen_ms", "last_seen_ms"),
    "person_names": ("id", "person_id", "name", "kind", "source", "hidden", "evidence",
                     "first_seen_ms", "last_seen_ms"),
    "person_decisions": ("id", "person_a", "person_b", "decision", "note", "decided_ms"),
    "suggestions": ("id", "kind", "person_a", "person_b", "field", "value", "evidence", "status",
                    "created_ms", "decided_ms"),
}


def _audit_triggers() -> list[str]:
    sql = []
    who = "coalesce((SELECT name FROM writer_context WHERE id = 1), 'manual')"
    for table, cols in _AUDITED.items():
        def obj(ref: str, cols: tuple[str, ...] = cols) -> str:
            return "json_object(" + ", ".join(f"'{c}', {ref}.{c}" for c in cols) + ")"
        for op, when, old, new, rowid in (
            ("insert", "AFTER INSERT", "NULL", obj("NEW"), "NEW.id"),
            ("update", "AFTER UPDATE", obj("OLD"), obj("NEW"), "NEW.id"),
            ("delete", "AFTER DELETE", obj("OLD"), "NULL", "OLD.id"),
        ):
            sql.append(
                f"CREATE TRIGGER IF NOT EXISTS {table}_audit_{op} {when} ON {table} BEGIN "
                f"INSERT INTO change_log(writer, tbl, row_id, op, old, new) "
                f"VALUES ({who}, '{table}', {rowid}, '{op}', {old}, {new}); END;"
            )
    return sql


def people_db_path(*, create: bool = False) -> Path:
    path = get_operational_data_path() / "people" / "people.db"
    if create:
        _ensure_private_dir(path.parent)
    return path


def _ensure_private_dir(directory: Path) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)


def open_people_db(path: Path, *, readonly: bool = False,
                   busy_timeout_ms: int = 2000, immutable: bool = False) -> sqlite3.Connection:
    """Open people.db; a writable open creates and migrates the schema (autocommit mode)."""
    if readonly:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro" + ("&immutable=1" if immutable else ""),
                               uri=True, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        return conn
    _ensure_private_dir(path.parent)
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        os.chmod(path, 0o600)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _migrate(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    has_meta = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'").fetchone()
    if has_meta:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        if row is not None:
            version = int(row[0])
            if version > SCHEMA_VERSION:
                raise RuntimeError(f"people.db schema {version} is newer than {SCHEMA_VERSION}")
            return
    conn.execute("BEGIN IMMEDIATE")
    try:
        for statement in _split(_DDL):
            conn.execute(statement)
        for statement in _audit_triggers():
            conn.execute(statement)
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
                     (str(SCHEMA_VERSION),))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _split(script: str) -> list[str]:
    """Split DDL into statements; trigger bodies end with 'END;'."""
    statements, buffer = [], ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            if buffer.strip():
                statements.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        statements.append(buffer.strip())
    return statements


@contextmanager
def writer(conn: sqlite3.Connection, name: str) -> Iterator[None]:
    """Label every change in the current transaction with ``name`` in change_log."""
    if not conn.in_transaction:
        raise RuntimeError("writer() needs an open transaction")
    conn.execute("UPDATE writer_context SET name = ? WHERE id = 1", (name,))
    try:
        yield
    finally:
        conn.execute("UPDATE writer_context SET name = NULL WHERE id = 1")
