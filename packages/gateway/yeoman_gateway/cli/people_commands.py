"""Person directory CLI: check, status, sync dry-run and the one-time Knowledge import."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import time
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from typing import Any

import typer

from .core import app, console

people_app = typer.Typer(help="Person directory")
app.add_typer(people_app, name="people")


class _DryRunRollbackError(Exception):
    """Raised inside a transaction to discard a dry run."""


def _print(data: Any, json_output: bool) -> None:
    if json_output:
        typer.echo(json.dumps(data, sort_keys=True))
    else:
        console.print(data)


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _copy_people_db(source: Path, target: Path) -> None:
    """Write a working copy of people.db (or an empty schema when it does not exist yet)."""
    from yeoman_gateway.people.schema import open_people_db

    if source.is_file():
        with closing(sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)) as conn:
            conn.execute("PRAGMA query_only=ON")
            target.write_bytes(conn.serialize())
    open_people_db(target).close()


def _status(conn: sqlite3.Connection) -> dict[str, Any]:
    def grouped(sql: str) -> dict[str, int]:
        return {str(k): int(v) for k, v in conn.execute(sql)}

    meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    report = meta.get("last_sync_report")
    return {
        "persons": grouped("SELECT effective_level, count(*) FROM persons_v "
                           "WHERE merged_into IS NULL GROUP BY 1"),
        "merged": int(conn.execute("SELECT count(*) FROM persons WHERE merged_into IS NOT NULL").fetchone()[0]),
        "ids": grouped("SELECT kind || ':' || CASE WHEN hidden = 1 THEN 'hidden' "
                       "WHEN valid_until_ms IS NOT NULL THEN 'ended' ELSE 'active' END, count(*) "
                       "FROM person_ids GROUP BY 1"),
        "names": grouped("SELECT source, count(*) FROM person_names WHERE hidden = 0 GROUP BY 1"),
        "open_suggestions": int(conn.execute(
            "SELECT count(*) FROM suggestions WHERE status = 'open'").fetchone()[0]),
        "last_sync_ms": int(meta["last_sync_ms"]) if "last_sync_ms" in meta else None,
        "last_sync_report": json.loads(report) if report else None,
    }


@people_app.command("check")
def people_check(json_output: bool = typer.Option(False, "--json", help="Output JSON")) -> None:
    """Read-only integrity check. Exit code 1 on any error finding."""
    from yeoman_gateway.people.check import check_people_db
    from yeoman_gateway.people.schema import open_people_db, people_db_path

    path = people_db_path()
    if not path.is_file():
        typer.echo("No person directory yet.", err=True)
        raise typer.Exit(1)
    with closing(open_people_db(path, readonly=True)) as conn:
        findings = check_people_db(conn)
    if json_output:
        typer.echo(json.dumps([asdict(f) for f in findings]))
    elif not findings:
        typer.echo("clean")
    else:
        for f in findings:
            typer.echo(f"{f.severity.upper()} {f.code} {f.table}#{f.row_id}: {f.detail}")
    if any(f.severity == "error" for f in findings):
        raise typer.Exit(1)


@people_app.command("status")
def people_status(json_output: bool = typer.Option(False, "--json", help="Output JSON")) -> None:
    """Counts only: persons by level, ids, names, suggestions and the last sync."""
    from yeoman_gateway.people.schema import open_people_db, people_db_path

    path = people_db_path()
    if not path.is_file():
        typer.echo("No person directory yet.", err=True)
        raise typer.Exit(1)
    with closing(open_people_db(path, readonly=True)) as conn:
        _print(_status(conn), json_output)


@people_app.command("sync")
def people_sync(
    dry_run: bool = typer.Option(False, "--dry-run", help="Run on a temporary copy; required"),
    history_db: Path | None = typer.Option(None, "--history-db", help="history.db to read"),
    json_output: bool = typer.Option(False, "--json", help="Output JSON"),
) -> None:
    """Preview one sync pass on a copy of the directory. The gateway runs the real sync."""
    from yeoman_gateway.history.live import history_db_path
    from yeoman_gateway.people.history_source import read_observed_persons
    from yeoman_gateway.people.schema import open_people_db, people_db_path
    from yeoman_gateway.people.store import PeopleStore
    from yeoman_gateway.people.sync import sync_from_history

    if not dry_run:
        typer.echo("The gateway runs the sync; use --dry-run to preview it.", err=True)
        raise typer.Exit(2)
    source = (history_db or history_db_path()).expanduser()
    if not source.is_file():
        typer.echo("history.db not found.", err=True)
        raise typer.Exit(1)
    with closing(sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)) as history:
        history.execute("PRAGMA query_only=ON")
        history.execute("BEGIN")                     # one consistent read
        observed = read_observed_persons(history)
    with tempfile.TemporaryDirectory(prefix="people-dry-run-") as tmp:
        copy = Path(tmp) / "people.db"
        _copy_people_db(people_db_path(), copy)
        with closing(open_people_db(copy)) as conn:
            store = PeopleStore(conn)
            with store.transaction("sync"):
                report = sync_from_history(store, observed, _now_ms())
            _print({"report": report.as_dict(), "status": _status(conn)}, json_output)


@people_app.command("import-knowledge")
def people_import_knowledge(
    dry_run: bool = typer.Option(False, "--dry-run", help="Report what the import would do"),
    confirm: bool = typer.Option(False, "--confirm", help="Snapshot, then import for real"),
    knowledge_db: Path | None = typer.Option(None, "--knowledge-db", help="Knowledge database to read"),
    json_output: bool = typer.Option(False, "--json", help="Output JSON"),
) -> None:
    """One-time import of the curated Knowledge contacts (names, preferred names, notes)."""
    from yeoman_gateway.knowledge.api import export_directory_seed
    from yeoman_gateway.people.backup import snapshot_people_db
    from yeoman_gateway.people.knowledge_import import import_knowledge_seed
    from yeoman_gateway.people.schema import open_people_db, people_db_path
    from yeoman_gateway.people.store import PeopleStore

    if dry_run == confirm:
        typer.echo("Pass exactly one of --dry-run or --confirm.", err=True)
        raise typer.Exit(2)
    if knowledge_db is None:
        from yeoman_shared.config.loader import load_config

        knowledge_db = Path(load_config().knowledge.db_path)
    knowledge_db = knowledge_db.expanduser()
    if not knowledge_db.is_file():
        typer.echo("Knowledge database not found.", err=True)
        raise typer.Exit(1)
    seed = export_directory_seed(knowledge_db)
    path = people_db_path()
    if dry_run:
        with tempfile.TemporaryDirectory(prefix="people-import-") as tmp:
            copy = Path(tmp) / "people.db"
            _copy_people_db(path, copy)
            with closing(open_people_db(copy)) as conn:
                store = PeopleStore(conn)
                try:
                    with store.transaction("import"):
                        report = import_knowledge_seed(store, seed, _now_ms())
                        raise _DryRunRollbackError
                except _DryRunRollbackError:
                    pass
        _print({"dry_run": True, "report": report.as_dict()}, json_output)
        return
    path = people_db_path(create=True)
    with closing(open_people_db(path)) as conn:
        snapshot = snapshot_people_db(path, label="people-pre-import")
        store = PeopleStore(conn)
        with store.transaction("import"):
            report = import_knowledge_seed(store, seed, _now_ms())
    _print({"dry_run": False, "snapshot": snapshot is not None, "report": report.as_dict()}, json_output)
