"""Raw message archive CLI. Purge is owner-only and needs confirmation."""

from __future__ import annotations

import getpass
import json
import sqlite3
import tempfile
from collections import Counter
from contextlib import closing
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import typer

from .core import app, console

raw_app = typer.Typer(help="Append-only raw message archive")
app.add_typer(raw_app, name="raw")


@raw_app.command("status")
def raw_status(json_output: bool = typer.Option(False, "--json", help="Output JSON")) -> None:
    """Show writer state, file and line counts."""
    from yeoman_shared.raw_archive.paths import raw_root
    from yeoman_shared.raw_archive.records import archive_files, file_digest
    from yeoman_shared.raw_archive.writer import STATUS_FILE, read_start_ms
    from yeoman_shared.utils.helpers import get_run_path

    root = raw_root()
    files = archive_files(root)
    lines = sum(file_digest(path)[1] for path in files)
    try:
        writer = json.loads((get_run_path() / STATUS_FILE).read_text(encoding="utf-8"))
    except OSError, ValueError:
        writer = {"state": "unknown"}
    data = {
        "root": str(root),
        "started_ms": read_start_ms(root) or 0,
        "files": len(files),
        "lines": lines,
        "writer": writer,
    }
    if json_output:
        typer.echo(json.dumps(data))
    else:
        console.print(data)


@raw_app.command("verify")
def raw_verify() -> None:
    """Seal finished months and check integrity. Exit code 1 on any problem."""
    from yeoman_shared.raw_archive.verify import verify_archive

    report = verify_archive()
    for problem in report.problems:
        typer.echo(f"PROBLEM {problem}")
    typer.echo(
        f"files={report.files_checked} lines={report.lines_total} closed={len(report.closed)}"
    )
    raise typer.Exit(0 if report.ok else 1)


@raw_app.command("check-capture")
def raw_check_capture() -> None:
    """Compare sent effects and transport receipts with raw outbound capture."""
    from yeoman_shared.raw_archive.paths import raw_root
    from yeoman_shared.raw_archive.records import archive_files, iter_records
    from yeoman_shared.raw_archive.writer import read_start_ms
    from yeoman_shared.utils.helpers import get_operational_data_path

    root = raw_root()
    start_ms = read_start_ms(root)
    missing_receipts = missing_results = missing_requests = 0
    missing_provider_ids = ambiguous_provider_ids = ambiguous_receipts = 0
    channel_counts: Counter[str] = Counter()
    kind_counts: Counter[str] = Counter()
    if start_ms is None:
        typer.echo("status=failed start_marker=missing effects=0 receipts=0 outbound_results=0 "
                   "missing_receipts=0 missing_results=0 missing_requests=0 "
                   "missing_provider_ids=0 ambiguous_provider_ids=0 ambiguous_receipts=0 "
                   "channels={} effect_kinds={}")
        raise typer.Exit(1)

    # Only payloads transported as addressable messages have a provider message ID.
    # Delete and external actions have no addressable outbound message result.
    eligible_kinds = {"text", "media", "forward", "reaction"}
    effect_meta: dict[str, tuple[str, str]] = {}
    effect_receipts: Counter[str] = Counter()
    receipts: Counter[tuple[str, str, str, str]] = Counter()
    request_counts: Counter[tuple[str, str, str]] = Counter()
    result_counts: Counter[tuple[str, str, str]] = Counter()
    raw_result_pairs: Counter[tuple[str, str, str]] = Counter()
    raw_results = 0
    try:
        db_path = get_operational_data_path() / "processing" / "processing.db"
        db_uri = f"{db_path.resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(db_uri, uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            for row in connection.execute(
                "SELECT effect_id, payload_kind, target_json FROM effects "
                "WHERE state = 'sent'"
            ):
                kind = str(row["payload_kind"] or "")
                if kind not in eligible_kinds:
                    continue
                target = json.loads(row["target_json"] or "{}")
                channel = str(target.get("channel") or "whatsapp")
                chat_id = str(target.get("chat_id") or "")
                effect_id = str(row["effect_id"])
                effect_meta[effect_id] = (channel, chat_id)
                channel_counts[channel] += 1
                kind_counts[kind] += 1
            for row in connection.execute(
                "SELECT effect_id, channel, chat_id, provider_message_id "
                "FROM transport_receipts WHERE confirmed_ms IS NOT NULL"
            ):
                effect_id = str(row["effect_id"])
                if effect_id in effect_meta:
                    effect_receipts[effect_id] += 1
                    channel, chat_id = str(row["channel"]), str(row["chat_id"])
                    provider_id = str(row["provider_message_id"] or "")
                    if effect_meta[effect_id] != (channel, chat_id):
                        missing_results += 1
                    receipts[(effect_id, channel, chat_id, provider_id)] += 1

        for path in archive_files(root):
            for _, record, _ in iter_records(path):
                if record is None or int(record.get("received_ms") or 0) < start_ms:
                    continue
                channel = str(record.get("channel") or "")
                if channel not in {"whatsapp", "telegram"} or record.get("direction") != "out":
                    continue
                kind = str(record.get("kind") or "")
                native = record.get("native")
                native = native if isinstance(native, dict) else {}
                correlation = str(record.get("correlation_id") or native.get("requestId") or "")
                chat_id = str(record.get("chat_id") or "")
                if kind == "outbound_request":
                    request_counts[(channel, chat_id, correlation)] += 1
                elif kind == "outbound_result":
                    raw_results += 1
                    raw_result_pairs[(channel, chat_id, correlation)] += 1
                    result_payload = native.get("result")
                    if not isinstance(result_payload, dict):
                        result_payload = {}
                    if channel == "whatsapp":
                        provider_id = str(result_payload.get("providerMessageId") or record.get("native_id") or "")
                    else:
                        provider_id = str(native.get("message_id") or record.get("native_id") or "")
                    if not provider_id:
                        missing_provider_ids += 1
                    else:
                        result_counts[(channel, chat_id, provider_id)] += 1

        for (channel, chat_id, correlation), count in raw_result_pairs.items():
            if not correlation or request_counts[(channel, chat_id, correlation)] != 1:
                missing_requests += count
        ambiguous_provider_ids = sum(count > 1 for count in result_counts.values())
        for effect_id, (channel, chat_id) in effect_meta.items():
            receipt_count = effect_receipts[effect_id]
            if receipt_count != 1:
                missing_receipts += receipt_count == 0
                ambiguous_receipts += receipt_count > 1
        for (effect_id, channel, chat_id, provider_id), count in receipts.items():
            if count != 1:
                continue
            if not provider_id:
                missing_provider_ids += 1
            elif result_counts[(channel, chat_id, provider_id)] != 1:
                missing_results += 1

        status = "ok" if not any((missing_receipts, missing_results, missing_requests,
                                  missing_provider_ids, ambiguous_provider_ids,
                                  ambiguous_receipts)) else "failed"
        typer.echo(
            f"status={status} start_marker=present effects={len(effect_meta)} "
            f"receipts={sum(effect_receipts.values())} outbound_results={raw_results} "
            f"missing_receipts={missing_receipts} missing_results={missing_results} "
            f"missing_requests={missing_requests} missing_provider_ids={missing_provider_ids} "
            f"ambiguous_provider_ids={ambiguous_provider_ids} "
            f"ambiguous_receipts={ambiguous_receipts} "
            f"channels={json.dumps(dict(channel_counts), sort_keys=True)} "
            f"effect_kinds={json.dumps(dict(kind_counts), sort_keys=True)}"
        )
        raise typer.Exit(0 if status == "ok" else 1)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
        typer.echo("status=failed start_marker=present error=unreadable_input "
                   "missing_receipts=0 missing_results=0 missing_requests=0 "
                   "missing_provider_ids=0 ambiguous_provider_ids=0 ambiguous_receipts=0")
        raise typer.Exit(1) from None


@raw_app.command("seed")
def raw_seed(dry_run: bool = typer.Option(False, "--dry-run", help="Count only")) -> None:
    """One-time import of history older than the archive START marker."""
    from yeoman_shared.raw_archive.paths import raw_root

    from yeoman_gateway.storage.raw_seed import SeedPaths, seed_raw_archive

    report = seed_raw_archive(raw_root(), SeedPaths.default(), dry_run=dry_run)
    typer.echo(json.dumps(asdict(report), indent=2, sort_keys=True))


@raw_app.command("rebuild-drill")
def raw_rebuild_drill(
    channel: str = typer.Option(..., "--channel"),
    chat: str = typer.Option(..., "--chat"),
    target: Path | None = typer.Option(
        None, "--target", help="Empty directory; default: a temp dir"
    ),
    compare_live: bool = typer.Option(
        False, "--compare-live", help="Compare with the live journal"
    ),
) -> None:
    """Replay one chat from the raw archive into an empty journal."""
    from yeoman_shared.raw_archive.paths import raw_root
    from yeoman_shared.utils.helpers import get_operational_data_path

    from yeoman_gateway.storage.raw_rebuild import rebuild_chat

    home = target or Path(tempfile.mkdtemp(prefix="yeoman-rebuild-"))
    live = get_operational_data_path() / "processing" / "processing.db" if compare_live else None
    report = rebuild_chat(
        raw_root(),
        channel=channel,
        chat_id=chat,
        target_home=home,
        live_processing_db=live,
    )
    typer.echo(json.dumps({**asdict(report), "target": str(home)}, indent=2))
    if compare_live and (report.missing_vs_live or report.extra_vs_live):
        raise typer.Exit(1)


@raw_app.command("purge")
def raw_purge(
    channel: str = typer.Option(..., "--channel"),
    chat: str | None = typer.Option(None, "--chat"),
    message: str | None = typer.Option(None, "--message"),
    before: str | None = typer.Option(None, "--before", help="ISO date, e.g. 2026-09-01"),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt"),
) -> None:
    """Physically delete lines from the raw archive. Owner only; writes an AUDIT record."""
    from yeoman_shared.raw_archive.paths import raw_root
    from yeoman_shared.raw_archive.purge import PurgeSelector, plan_purge, purge

    before_ms = int(datetime.fromisoformat(before).timestamp() * 1000) if before else None
    selector = PurgeSelector(channel=channel, chat_id=chat, native_id=message, before_ms=before_ms)
    try:
        plan = plan_purge(raw_root(), selector)
    except ValueError as exc:
        typer.echo(f"Refused: {exc}")
        raise typer.Exit(2) from exc
    typer.echo(
        f"Would remove {plan.removed_lines} line(s) from {len(plan.files)} file(s) "
        f"and {len(plan.media_removed)} media file(s)."
    )
    if not yes and not typer.confirm("Permanently apply this purge disposition?", default=False):
        raise typer.Exit(1)
    result = purge(raw_root(), selector, operator=getpass.getuser())
    if result.removed_lines:
        typer.echo(f"Removed {result.removed_lines} line(s); AUDIT updated.")
    else:
        typer.echo("Recorded purge disposition; 0 archived lines removed.")
