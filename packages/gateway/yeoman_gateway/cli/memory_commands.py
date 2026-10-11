"""History-backed Knowledge reads and memory maintenance reports."""

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

memory_app = typer.Typer(help="Inspect history-backed Knowledge and media")
app.add_typer(memory_app, name="memory")


@memory_app.command("search")
def memory_search(
    query: str = typer.Option(..., "--query", "-q", help="Search query"),
    chat_id: str = typer.Option(..., "--chat-id", help="WhatsApp chat id"),
    limit: int = typer.Option(8, "--limit", "-n", min=1, max=100),
) -> None:
    """Search Knowledge in one explicit WhatsApp chat."""
    from yeoman_gateway.history.export import cli_secondary_request

    result = cli_secondary_request("knowledge_read", dict(chat_id=chat_id, query=query, limit=limit))
    console.print("\n".join(result["statements"]) or "No Knowledge hits.")


@memory_app.command("lineage-inventory")
def memory_lineage_inventory(
    inbound_dir: str = typer.Option(..., "--inbound-dir", help="Isolated inbound JSONL snapshot"),
    processing_db: str = typer.Option(..., "--processing-db", help="Isolated ProcessingStore snapshot"),
    session_state_dir: str = typer.Option(..., "--session-state-dir", help="Isolated session JSONL snapshot"),
    knowledge_db: str = typer.Option(..., "--knowledge-db", help="Isolated Knowledge snapshot"),
    media_root: str = typer.Option(..., "--media-root", help="Isolated media snapshot"),
    apply: bool = typer.Option(False, "--apply", help="Apply the import instead of reporting the dry run"),
    allow_model_jobs: bool = typer.Option(False, "--allow-model-jobs", help="Schedule explicitly approved derived model work"),
) -> None:
    """Inventory isolated lineage snapshots and optionally import them."""
    from yeoman_gateway.history.export import require_isolated_paths
    from yeoman_gateway.knowledge.api import import_lineage, inspect_lineage_sources

    paths = tuple(Path(item).expanduser() for item in (inbound_dir, processing_db, session_state_dir, knowledge_db, media_root))
    require_isolated_paths(*paths)
    inventory = inspect_lineage_sources(
        inbound_dir=inbound_dir,
        processing_db=processing_db,
        session_state_dir=session_state_dir,
        knowledge_db=knowledge_db,
        media_root=media_root,
    )
    table = Table(title="Lineage inventory (metadata only)")
    table.add_column("Decision")
    table.add_column("Rows", justify="right")
    for decision, count in sorted(inventory.counts().items()):
        table.add_row(decision, str(count))
    table.add_row("eligible model jobs", str(inventory.eligible_model_jobs))
    console.print(table)
    if not apply:
        console.print("[yellow]Dry run:[/yellow] nothing was imported or scheduled.")
        return
    report = import_lineage(inventory, apply=True, allow_model_jobs=allow_model_jobs)
    console.print(
        f"[green]✓[/green] imported {report.imported}, quarantined {report.quarantined}, "
        f"scheduled {report.model_jobs_scheduled} of {report.eligible_model_jobs} eligible job(s)."
    )


@memory_app.command("source-accounting")
def memory_source_accounting() -> None:
    from yeoman_gateway.history.export import cli_secondary_request

    console.print(json.dumps(cli_secondary_request("knowledge_accounting", {}), sort_keys=True))


MEDIA_AGE_BUCKETS: tuple[tuple[int | None, str], ...] = (
    (7, "0-7d"), (30, "8-30d"), (90, "31-90d"), (None, ">90d"),
)
_DAY_MS = 86_400_000


@dataclass(frozen=True, slots=True)
class MediaGrowthRow:
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
    return MEDIA_AGE_BUCKETS[-1][1]


def media_growth_rows(
    references: Iterable[Mapping[str, Any]], *, now_ms: int, stat_size: Callable[[str], int | None]
) -> tuple[MediaGrowthRow, ...]:
    """Group media metadata by kind, age and presence; never open media files."""
    grouped: dict[tuple[str, str, str], list[int]] = {}
    for reference in references:
        kind = str(reference.get("kind") or "unknown")
        bucket = media_age_bucket(int(now_ms) - int(reference.get("occurred_ms") or 0))
        size = stat_size(str(reference.get("path") or ""))
        grouped.setdefault((kind, bucket, "missing" if size is None else "present"), []).append(int(size or 0))
    return tuple(
        MediaGrowthRow(kind, bucket, state, len(sizes), sum(sizes))
        for (kind, bucket, state), sizes in sorted(grouped.items())
    )


def _media_size_only(path: str) -> int | None:
    if not path:
        return None
    try:
        return int(os.stat(path).st_size)
    except OSError:
        return None


def collect_media_references(processing_db: Path) -> list[dict[str, Any]]:
    """Read media paths and metadata from ProcessingStore without opening payloads."""
    if not processing_db.exists():
        return []
    connection = sqlite3.connect(f"file:{processing_db}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT kind, occurred_ms, payload_json FROM events WHERE payload_json LIKE '%media%'"
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        connection.close()
    references = []
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"] or "{}"))
        except (TypeError, ValueError):
            continue
        media = payload.get("media") if isinstance(payload, dict) else None
        if isinstance(media, dict):
            references.append({
                "kind": str(media.get("kind") or media.get("mimeType") or "unknown"),
                "occurred_ms": int(row["occurred_ms"] or 0),
                "path": str(media.get("path") or ""),
            })
    return references


@memory_app.command("media-growth")
def memory_media_growth(
    processing_db: str = typer.Option(..., "--processing-db", help="ProcessingStore database"),
) -> None:
    """Report media growth by kind, age and presence. Read-only, deletes nothing."""
    rows = media_growth_rows(
        collect_media_references(Path(processing_db)), now_ms=int(time.time() * 1000), stat_size=_media_size_only
    )
    table = Table(title="Media growth (read-only, no deletion)")
    table.add_column("Kind")
    table.add_column("Age")
    table.add_column("State")
    table.add_column("Files", justify="right")
    table.add_column("Bytes", justify="right")
    for row in rows:
        table.add_row(row.media_kind, row.age_bucket, row.state, str(row.count), str(row.bytes))
    console.print(table)


statements_app = typer.Typer(help="Live owner Knowledge statement inspection and curation")
memory_app.add_typer(statements_app, name="statements")


@statements_app.command("list")
def memory_statements_list(limit: int = typer.Option(50, "--limit", min=1, max=100)) -> None:
    from yeoman_gateway.history.export import cli_secondary_request

    console.print(json.dumps(cli_secondary_request("knowledge_statements", dict(action="list", limit=limit))))


@statements_app.command("show")
def memory_statements_show(statement_id: str, content: bool = typer.Option(False, "--content")) -> None:
    from yeoman_gateway.history.export import cli_secondary_request

    console.print(json.dumps(cli_secondary_request("knowledge_statements", dict(action="show", statement_id=statement_id, content=content))))


@statements_app.command("erase")
def memory_statements_erase(statement_id: str) -> None:
    from yeoman_gateway.history.export import cli_secondary_request

    console.print(json.dumps(cli_secondary_request("knowledge_statements", dict(action="erase", statement_id=statement_id))))
