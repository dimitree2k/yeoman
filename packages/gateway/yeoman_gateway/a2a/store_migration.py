"""Explicit, source-read-only merge of legacy A2A SQLite stores."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote


@dataclass(frozen=True)
class A2AStoreMigrationReport:
    target: str
    table_counts: dict[str, int]
    primary_keys: dict[str, list[Any]]
    row_sha256: dict[str, str]
    source_sha256: dict[str, str]

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)


def _digest_rows(rows: list[tuple[Any, ...]]) -> str:
    def json_value(value: Any) -> Any:
        if isinstance(value, bytes):
            return {"bytes_hex": value.hex()}
        return value

    payload = json.dumps(
        [[json_value(value) for value in row] for row in rows],
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _open_source(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise ValueError(f"source database does not exist: {path}")
    for suffix in ("-wal", "-journal"):
        sidecar = path.with_name(path.name + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise ValueError(f"source has non-empty {suffix}; checkpoint it before migration")
    uri = f"file:{quote(str(path.resolve()))}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    try:
        connection.row_factory = sqlite3.Row
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError(f"source database failed integrity_check: {path}")
    except BaseException:
        connection.close()
        raise
    return connection


def migrate_a2a_stores(
    relay_source: str | Path,
    research_source: str | Path,
    target: str | Path,
) -> A2AStoreMigrationReport:
    """Copy all user tables from two clean snapshots; never mutate either source."""
    sources = [Path(relay_source).expanduser().resolve(), Path(research_source).expanduser().resolve()]
    destination = Path(target).expanduser().resolve()
    if sources[0] == sources[1] or destination in sources:
        raise ValueError("sources and target must be three distinct paths")
    if destination.exists():
        raise ValueError(f"target already exists: {destination}")
    source_hashes = {str(path): _file_sha256(path) for path in sources}
    source_connections: list[sqlite3.Connection] = []
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    target_connection: sqlite3.Connection | None = None
    try:
        for path in sources:
            source_connections.append(_open_source(path))
        required_tables = (
            {"tasks", "idempotency", "artifacts"},
            {"pending_research", "completed_reports"},
        )
        for path, connection, required in zip(
            sources, source_connections, required_tables, strict=True
        ):
            actual = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'"
                )
            }
            if not required <= actual:
                missing = ", ".join(sorted(required - actual))
                raise ValueError(f"source is missing required A2A tables ({missing}): {path}")
        schemas: list[tuple[sqlite3.Connection, str, str]] = []
        tables: dict[str, tuple[sqlite3.Connection, str]] = {}
        for connection in source_connections:
            for row in connection.execute(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE type IN ('table','index','trigger','view') AND sql IS NOT NULL "
                "AND name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ):
                kind, name, sql = str(row["type"]), str(row["name"]), str(row["sql"])
                if kind == "table":
                    if name in tables:
                        raise ValueError(f"source table collision: {name}")
                    tables[name] = (connection, sql)
                schemas.append((connection, kind, sql))
        if not tables:
            raise ValueError("sources contain no A2A tables")
        destination.parent.mkdir(parents=True, exist_ok=True)
        target_connection = sqlite3.connect(temporary)
        target_connection.execute("PRAGMA foreign_keys=ON")
        target_connection.execute("BEGIN IMMEDIATE")
        target_connection.execute("PRAGMA defer_foreign_keys=ON")
        # Create all tables before copying; indexes, triggers and views follow afterward.
        for connection, kind, sql in schemas:
            if kind == "table":
                target_connection.execute(sql)
        counts: dict[str, int] = {}
        keys: dict[str, list[Any]] = {}
        row_hashes: dict[str, str] = {}
        for table, (source, _sql) in sorted(tables.items()):
            name = _quote(table)
            columns = [str(row["name"]) for row in source.execute(f"PRAGMA table_info({name})")]
            primary_key = [
                str(row["name"])
                for row in sorted(
                    (row for row in source.execute(f"PRAGMA table_info({name})") if int(row["pk"])),
                    key=lambda row: int(row["pk"]),
                )
            ]
            if not columns or not primary_key:
                raise ValueError(f"table has no columns or primary key: {table}")
            source_rows = source.execute(f"SELECT * FROM {name}").fetchall()
            values = [tuple(row[column] for column in columns) for row in source_rows]
            target_connection.executemany(
                f"INSERT INTO {name} ({', '.join(_quote(column) for column in columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                values,
            )
            key_columns = ", ".join(_quote(column) for column in primary_key)
            source_keys = [
                row[0] if len(primary_key) == 1 else list(row)
                for row in source.execute(f"SELECT {key_columns} FROM {name} ORDER BY {key_columns}")
            ]
            target_rows = target_connection.execute(f"SELECT * FROM {name}").fetchall()
            target_values = [tuple(row) for row in target_rows]
            if len(values) != len(target_values) or _digest_rows(values) != _digest_rows(target_values):
                raise ValueError(f"row fidelity check failed for table: {table}")
            counts[table] = len(values)
            keys[table] = source_keys
            row_hashes[table] = _digest_rows(values)
        for _connection, kind, sql in schemas:
            if kind != "table":
                target_connection.execute(sql)
        if target_connection.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("target foreign_key_check failed")
        target_connection.commit()
        target_connection.close()
        target_connection = None
        for path, expected in zip(sources, source_hashes.values(), strict=True):
            if _file_sha256(path) != expected:
                raise ValueError(f"source changed during migration: {path}")
        # Hard-link publication cannot overwrite a target created by a concurrent run.
        os.link(temporary, destination)
        return A2AStoreMigrationReport(
            target=str(destination),
            table_counts=counts,
            primary_keys=keys,
            row_sha256=row_hashes,
            source_sha256=source_hashes,
        )
    finally:
        if target_connection is not None:
            target_connection.close()
        temporary.unlink(missing_ok=True)
        temporary.with_name(temporary.name + "-wal").unlink(missing_ok=True)
        temporary.with_name(temporary.name + "-shm").unlink(missing_ok=True)
        for connection in source_connections:
            connection.close()


__all__ = ["A2AStoreMigrationReport", "migrate_a2a_stores"]
