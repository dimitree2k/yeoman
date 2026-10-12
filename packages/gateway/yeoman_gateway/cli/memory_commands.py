"""Bounded Knowledge reads, statement curation and offline inventories."""


from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer
from rich.table import Table

from .core import app, console

memory_app = typer.Typer(help="Manage Knowledge")
app.add_typer(memory_app, name="memory")
MEMORY_SCOPES = {"chat", "user", "global", "all"}
statements_app = typer.Typer(help="Live owner Knowledge statement inspection and curation")
memory_app.add_typer(statements_app, name="statements")


def _normalize_choice(raw: str, *, choices: set[str], option: str) -> str:
    value = raw.strip().lower()
    if value not in choices:
        console.print(f"[red]Invalid {option}. Use: {'|'.join(sorted(choices))}[/red]")
        raise typer.Exit(1)
    return value


@memory_app.command("search")
def memory_search(
    query: str = typer.Option(..., "--query", "-q", help="Search query"),
    channel: str | None = typer.Option(None, "--channel", help="Channel for scoped search"),
    chat_id: str | None = typer.Option(None, "--chat-id", help="Chat id for scoped search"),
    sender_id: str | None = typer.Option(None, "--sender-id", help="Sender id for user scope"),
    scope: str = typer.Option("all", "--scope", help="chat|user|global|all"),
    limit: int = typer.Option(8, "--limit", "-n", min=1, max=100),
) -> None:
    """Search long-term memory with scope filters."""
    scope_value = _normalize_choice(scope, choices=MEMORY_SCOPES, option="--scope")
    from yeoman_gateway.history.export import cli_secondary_request
    if channel != 'whatsapp' or not chat_id or scope_value != 'chat':
        raise typer.BadParameter('explicit WhatsApp chat scope required')
    result = cli_secondary_request('knowledge_read', dict(chat_id=chat_id, query=query, limit=limit))
    console.print('\n'.join(result['statements']) or 'No memory hits.')
    return


@memory_app.command("lineage-inventory")
def memory_lineage_inventory(
    inbound_dir: str | None = typer.Option(
        None, "--inbound-dir", help="Directory of legacy inbound JSONL archives"
    ),
    processing_db: str | None = typer.Option(
        None, "--processing-db", help="Path to the ProcessingStore database"
    ),
    session_state_dir: str | None = typer.Option(
        None, "--session-state-dir", help="Directory of legacy session JSONL files"
    ),
    knowledge_db: str | None = typer.Option(
        None, "--knowledge-db", help="Path to an existing knowledge store"
    ),
    media_root: str | None = typer.Option(
        None, "--media-root", help="Directory of stored media references"
    ),
    apply: bool = typer.Option(
        False, "--apply", help="Apply the import instead of reporting the dry run"
    ),
    allow_model_jobs: bool = typer.Option(
        False,
        "--allow-model-jobs",
        help="Schedule derived model work; requires an explicit report of the eligible count",
    ),
) -> None:
    """Inventory legacy lineage sources (metadata only) and optionally import them.

    The default is a dry run: metadata, counts and schema only, never content, never a
    provider or model call, and never a PDF parse or OCR.  Derived model work is only
    scheduled with --allow-model-jobs, whose eligible count is always reported first.
    """

    from yeoman_gateway.history.export import require_isolated_paths
    from yeoman_gateway.knowledge.api import import_lineage, inspect_lineage_sources
    if not all((inbound_dir, processing_db, session_state_dir, knowledge_db, media_root)):
        raise typer.BadParameter('explicit isolated inventory paths required')
    require_isolated_paths(*(Path(p) for p in (inbound_dir, processing_db, session_state_dir, knowledge_db, media_root)))
    inventory = inspect_lineage_sources(
        inbound_dir=inbound_dir,
        processing_db=processing_db,
        session_state_dir=session_state_dir,
        knowledge_db=knowledge_db,
        media_root=media_root,
    )
    counts = inventory.counts()
    table = Table(title="Lineage inventory (metadata only)")
    table.add_column("Decision")
    table.add_column("Rows", justify="right")
    for decision, count in sorted(counts.items()):
        table.add_row(decision, str(count))
    table.add_row("eligible model jobs", str(inventory.eligible_model_jobs))
    console.print(table)

    if not apply:
        console.print(
            "[yellow]Dry run:[/yellow] nothing was imported, no model job was scheduled."
        )
        return
    report = import_lineage(inventory, apply=True, allow_model_jobs=allow_model_jobs)
    console.print(
        f"[green]✓[/green] imported {report.imported}, quarantined {report.quarantined}, "
        f"scheduled {report.model_jobs_scheduled} of {report.eligible_model_jobs} eligible job(s)."
    )


@memory_app.command('source-accounting')
def memory_source_accounting() -> None:
    from yeoman_gateway.history.export import cli_secondary_request
    result = cli_secondary_request('knowledge_accounting', {})
    console.print(json.dumps(result, sort_keys=True))


MEDIA_AGE_BUCKETS: tuple[tuple[int | None, str], ...] = (
    (7, "0-7d"),
    (30, "8-30d"),
    (90, "31-90d"),
    (None, ">90d"),
)


MEDIA_STATES: tuple[str, ...] = ("present", "missing")


_DAY_MS = 86_400_000


@dataclass(frozen=True, slots=True)
class MediaGrowthRow:
    """One (kind, age bucket, presence) group with its aggregate byte count."""

    media_kind: str
    age_bucket: str
    state: str
    count: int
    bytes: int


def media_age_bucket(age_ms: int) -> str:
    days = max(0, int(age_ms)) / _DAY_MS
    for limit, label in MEDIA_AGE_BUCKETS:
        if limit is None or days <= limit:
            return label
    return MEDIA_AGE_BUCKETS[-1][1]  # pragma: no cover - defensive


def media_growth_rows(
    references: Iterable[Mapping[str, Any]],
    *,
    now_ms: int,
    stat_size: Callable[[str], int | None],
) -> tuple[MediaGrowthRow, ...]:
    """Group stored media *metadata* into (kind, age, presence) buckets.

    ``stat_size`` returns the stored file's size in bytes, or ``None`` when the file is
    missing.  It is the only filesystem access this report performs: no file is opened,
    read, hashed, parsed or deleted.
    """
    grouped: dict[tuple[str, str, str], list[int]] = {}
    for reference in references:
        kind = str(reference.get("kind") or "unknown")
        occurred = int(reference.get("occurred_ms") or 0)
        bucket = media_age_bucket(int(now_ms) - occurred)
        size = stat_size(str(reference.get("path") or ""))
        state = "missing" if size is None else "present"
        grouped.setdefault((kind, bucket, state), []).append(int(size or 0))
    rows = [
        MediaGrowthRow(
            media_kind=kind,
            age_bucket=bucket,
            state=state,
            count=len(sizes),
            bytes=sum(sizes),
        )
        for (kind, bucket, state), sizes in grouped.items()
    ]
    rows.sort(key=lambda row: (row.media_kind, row.age_bucket, row.state))
    return tuple(rows)


def _media_size_only(path: str) -> int | None:
    """Size of a stored media file.  Never opens it."""
    if not path:
        return None
    try:
        return int(os.stat(path).st_size)
    except OSError:
        return None


def collect_media_references(processing_db: Path) -> list[dict[str, Any]]:
    """Media references from stored event metadata.  Payload text is never returned."""
    if not Path(processing_db).exists():
        return []
    connection = sqlite3.connect(f"file:{Path(processing_db)}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT kind, occurred_ms, payload_json FROM events"
            " WHERE payload_json LIKE '%media%'"
        ).fetchall()
    except sqlite3.Error:  # pragma: no cover - defensive
        return []
    finally:
        connection.close()
    references: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"] or "{}"))
        except (TypeError, ValueError):
            continue
        media = payload.get("media") if isinstance(payload, dict) else None
        if not isinstance(media, dict):
            continue
        references.append(
            {
                "kind": str(media.get("kind") or media.get("mimeType") or "unknown"),
                "occurred_ms": int(row["occurred_ms"] or 0),
                "path": str(media.get("path") or ""),
            }
        )
    return references


@memory_app.command("media-growth")
def memory_media_growth(
    processing_db: str = typer.Option(
        ..., "--processing-db", help="Path to the ProcessingStore database"
    ),
) -> None:
    """Report media growth by kind, age and presence.  Read-only, deletes nothing."""
    references = collect_media_references(Path(processing_db))
    rows = media_growth_rows(
        references, now_ms=int(time.time() * 1000), stat_size=_media_size_only
    )
    table = Table(title="Media growth (read-only, no deletion)")
    table.add_column("Kind")
    table.add_column("Age")
    table.add_column("State")
    table.add_column("Files", justify="right")
    table.add_column("Bytes", justify="right")
    for row in rows:
        table.add_row(
            row.media_kind, row.age_bucket, row.state, str(row.count), str(row.bytes)
        )
    console.print(table)


@statements_app.command('list')
def memory_statements_list(limit: int = typer.Option(50, '--limit', min=1, max=100)) -> None:
    from yeoman_gateway.history.export import cli_secondary_request
    console.print(json.dumps(cli_secondary_request('knowledge_statements', dict(action='list', limit=limit))))


@statements_app.command('show')
def memory_statements_show(statement_id: str, content: bool = typer.Option(False, '--content')) -> None:
    from yeoman_gateway.history.export import cli_secondary_request
    console.print(json.dumps(cli_secondary_request('knowledge_statements', dict(action='show', statement_id=statement_id, content=content))))


@statements_app.command('erase')
def memory_statements_erase(statement_id: str) -> None:
    from yeoman_gateway.history.export import cli_secondary_request
    console.print(json.dumps(cli_secondary_request('knowledge_statements', dict(action='erase', statement_id=statement_id))))

