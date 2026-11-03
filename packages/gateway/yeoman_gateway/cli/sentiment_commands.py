"""``yeoman sentiment report|run|validate``: owner-facing reception commands."""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import typer
from rich.table import Table

from .core import app, console

sentiment_app = typer.Typer(help="Reception measurement for Arvid's group messages")
app.add_typer(sentiment_app, name="sentiment")


def _store_read_only() -> Any:
    from yeoman_shared.utils.helpers import get_operational_store_path

    from yeoman_gateway.reception.store import SentimentStore

    path = get_operational_store_path("sentiment", create=False)
    if not path.exists():
        return None
    return SentimentStore(path, read_only=True)


def _policy_chat_maps() -> tuple[dict[str, str], list[str]]:
    """Reception-enabled chat ids and their human ``comment`` labels."""
    from yeoman_gateway.policy.loader import load_policy

    policy = load_policy()
    channel = policy.channels.get("whatsapp")
    if channel is None:
        return {}, []
    labels: dict[str, str] = {}
    ids: list[str] = []
    for chat_id, override in channel.chats.items():
        if not override.reception_metrics:
            continue
        ids.append(chat_id)
        labels[chat_id] = str(override.comment or chat_id)
    return labels, ids


def _policy_tuned_chat_maps() -> tuple[dict[str, str], list[str]]:
    """Reception-tuned chat ids and their human ``comment`` labels."""
    from yeoman_gateway.policy.loader import load_policy

    policy = load_policy()
    channel = policy.channels.get("whatsapp")
    if channel is None:
        return {}, []
    labels: dict[str, str] = {}
    ids: list[str] = []
    for chat_id, override in channel.chats.items():
        if not override.reception_metrics or not override.reception_tuning:
            continue
        ids.append(chat_id)
        labels[chat_id] = str(override.comment or chat_id)
    return labels, ids


@sentiment_app.command("report")
def sentiment_report(
    chat: str | None = typer.Option(
        None, "--chat", help="Chat id or the policy comment name"
    ),
    weeks: int = typer.Option(8, "--weeks", min=1, max=52, help="How many weeks to show"),
) -> None:
    """Read sentiment.db and print one table per chat; no gateway required."""
    from yeoman_gateway.reception.report import render_report, resolve_chat_selector

    store = _store_read_only()
    if store is None:
        console.print("Keine Empfangsdaten gespeichert (sentiment.db fehlt).")
        return
    try:
        labels, enabled = _policy_chat_maps()
        known = [item.chat_id for item in store.weekly()]
        chat_ids = list(dict.fromkeys([*known, *enabled]))
        if chat:
            selected = resolve_chat_selector(
                chat, chat_comments={key: value for key, value in labels.items()}, chat_ids=chat_ids
            )
            if selected is None:
                console.print(f"[red]Unbekannter Chat:[/red] {chat}")
                raise typer.Exit(2)
            chat_ids = [selected]
        elif not chat_ids:
            console.print("Keine Empfangsdaten gespeichert.")
            return
        console.print(render_report(
            store, chat_labels=labels, chat_ids=chat_ids, weeks=weeks
        ))
    finally:
        store.close()


async def _request_command(
    socket_path: Path, cmd: str, args: dict[str, Any], *, timeout: float = 600
) -> dict:
    writer = None
    try:
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        writer.write(json.dumps({"cmd": cmd, "args": args}).encode() + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=timeout)
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
    reply = json.loads(line)
    if not isinstance(reply, dict):
        raise ValueError("invalid reply")
    return reply


async def _request_sentiment_run(socket_path: Path, args: dict[str, str]) -> dict:
    return await _request_command(socket_path, "sentiment_run", args)


@sentiment_app.command("run")
def sentiment_run(
    since: str | None = typer.Option(
        None, "--since", help="Backfill from this date (YYYY-MM-DD); default 28 days"
    ),
    chat: str | None = typer.Option(None, "--chat", help="One chat id or comment name"),
) -> None:
    """Backfill/recompute P1 and P2 through the running gateway (owner-only)."""
    from yeoman_shared.config.loader import load_config

    try:
        start = date.fromisoformat(since) if since else (
            datetime.now(timezone.utc).date() - timedelta(days=28)
        )
    except ValueError:
        console.print("[red]--since must be YYYY-MM-DD[/red]")
        raise typer.Exit(2) from None
    labels, enabled = _policy_chat_maps()
    if chat:
        from yeoman_gateway.reception.report import resolve_chat_selector

        selected = resolve_chat_selector(
            chat, chat_comments=labels, chat_ids=enabled
        )
        if selected is None:
            console.print(f"[red]Unbekannter Chat:[/red] {chat}")
            raise typer.Exit(2)
        chat_id = selected
    else:
        chat_id = ""
    socket_path = load_config().ipc.resolved_gateway_socket_path
    try:
        reply = asyncio.run(_request_sentiment_run(
            socket_path, {"since": start.isoformat(), "chat": chat_id}
        ))
    except (OSError, ValueError, TimeoutError):
        console.print("[red]Gateway unavailable.[/red]")
        raise typer.Exit(1) from None
    if reply.get("status") != "ok":
        detail = reply.get("message") or reply.get("code") or reply.get("status")
        console.print(f"[red]Sentiment-Lauf fehlgeschlagen:[/red] {detail}")
        raise typer.Exit(1)
    console.print(
        f"[green]✓[/green] Empfang neu berechnet: "
        f"{reply.get('weeks', 0)} Wochen, {reply.get('labels', 0)} Labels"
    )
    failed_days = int(reply.get("failed_days") or 0)
    if failed_days:
        console.print(
            f"[yellow]{failed_days} Tage ohne Labels (Modell nicht erreichbar oder "
            "Antwort unbrauchbar), siehe gateway.log.[/yellow]"
        )
        raise typer.Exit(1)


@sentiment_app.command("validate")
def sentiment_validate(
    labels: Path = typer.Option(..., "--labels", help="CSV: message_id,expected_responds,expected_stance"),
) -> None:
    """Report labeler agreement for a coordinator-supplied CSV (synthetic in tests)."""
    from yeoman_gateway.reception.report import label_agreement, read_expected_csv

    store = _store_read_only()
    if store is None:
        console.print("Keine Labels gespeichert (sentiment.db fehlt).")
        raise typer.Exit(2)
    try:
        try:
            expected = read_expected_csv(labels)
        except (OSError, UnicodeError):
            console.print(f"[red]CSV nicht lesbar:[/red] {labels}")
            raise typer.Exit(2) from None
        actual = store.labels()
        report = label_agreement(expected=expected, actual=actual)
    finally:
        store.close()
    table = Table(title="Reception label agreement")
    table.add_column("metric")
    table.add_column("value", style="cyan")
    for key in (
        "rows", "missing_labels", "responds_total", "responds_accuracy",
        "stance_total", "stance_accuracy", "negative_accuracy",
    ):
        table.add_row(key, str(report[key]))
    console.print(table)
    console.print("responds_confusion " + json.dumps(report["responds_confusion"], sort_keys=True))
    console.print("stance_confusion " + json.dumps(report["stance_confusion"], sort_keys=True))


@sentiment_app.command("stance")
def sentiment_stance(
    chat: str | None = typer.Option(None, "--chat", help="Chat id or the policy comment name")
) -> None:
    """Read stance notes: one row per person and chat, with the counted patterns."""
    import sqlite3

    from yeoman_shared.config.loader import load_config

    labels, tuned = _policy_tuned_chat_maps()
    chat_id = ""
    if chat:
        from yeoman_gateway.reception.report import resolve_chat_selector

        selected = (
            chat if "@" in chat and chat.endswith("@g.us") else resolve_chat_selector(
                chat, chat_comments=labels, chat_ids=tuned
            )
        )
        if selected is None:
            console.print(f"[red]Unbekannter Chat:[/red] {chat}")
            raise typer.Exit(2)
        chat_id = selected
    path = load_config().knowledge.resolved_db_path
    if not Path(path).expanduser().is_file():
        console.print("Keine Wissensdatenbank gefunden.")
        return
    try:
        connection = sqlite3.connect(
            f"file:{Path(path).expanduser()}?mode=ro", uri=True
        )
    except sqlite3.Error:
        console.print("Keine Wissensdatenbank gefunden.")
        return
    try:
        query = (
            "SELECT n.chat_id, n.person_id, n.lines_json, n.built_ms,"
            " (SELECT COUNT(*) FROM knowledge_person_note_sources s"
            "  WHERE s.note_id = n.note_id) AS sources"
            " FROM knowledge_person_notes n"
            " WHERE n.channel='whatsapp' AND n.family='stance' AND n.status='active'"
            + (" AND n.chat_id=?" if chat_id else "")
            + " ORDER BY n.chat_id, n.person_id, n.built_ms DESC"
        )
        rows = connection.execute(query, (chat_id,) if chat_id else ()).fetchall()
    except sqlite3.Error:
        console.print("Keine Stance-Notizen gespeichert.")
        return
    finally:
        connection.close()
    latest: dict[tuple[str, str], tuple[str, int, list[str]]] = {}
    for room, person, lines_raw, built_ms, sources in rows:
        key = (str(room), str(person))
        if key in latest:
            continue
        try:
            payload = json.loads(str(lines_raw or "[]"))
        except ValueError:
            payload = []
        lines = [
            str(item.get("text") or "")
            for item in payload
            if isinstance(item, dict)
        ]
        name = next(
            (
                str(item.get("author") or "")
                for item in payload
                if isinstance(item, dict) and item.get("author")
            ),
            "",
        )
        latest[key] = (name or "?", int(sources or 0), lines)
    if not latest:
        console.print("Keine Stance-Notizen gespeichert.")
        return
    table = Table(title="Reception stance notes")
    for column in ("chat", "person", "replies", "patterns"):
        table.add_column(column)
    for (room, _person), (name, sources, lines) in sorted(latest.items()):
        table.add_row(room, name, str(sources), " · ".join(lines) or "-")
    console.print(table)


lessons_app = typer.Typer(help="Reception lessons: propose, show, accept, reject, rollback")
sentiment_app.add_typer(lessons_app, name="lessons")


def _lessons_chat_id(chat: str) -> str:
    labels, tuned = _policy_tuned_chat_maps()
    from yeoman_gateway.reception.report import resolve_chat_selector

    selected = (
        chat if "@" in chat and chat.endswith("@g.us") else resolve_chat_selector(
            chat, chat_comments=labels, chat_ids=tuned
        )
    )
    if selected is None:
        console.print(f"[red]Unbekannter Chat:[/red] {chat}")
        raise typer.Exit(2)
    return selected


def _lessons_command(cmd: str, args: dict[str, Any], *, timeout: float = 600) -> dict:
    from yeoman_shared.config.loader import load_config

    socket_path = load_config().ipc.resolved_gateway_socket_path
    try:
        return asyncio.run(_request_command(socket_path, cmd, args, timeout=timeout))
    except (OSError, ValueError, TimeoutError):
        console.print("[red]Gateway unavailable.[/red]")
        raise typer.Exit(1) from None


@lessons_app.command("propose")
def lessons_propose(
    chat: str | None = typer.Option(None, "--chat", help="One tuned chat id or comment")
) -> None:
    """Run the weekly lessons proposer through the gateway (owner-only)."""
    chat_id = _lessons_chat_id(chat) if chat else ""
    reply = _lessons_command("sentiment_lessons_propose", {"chat": chat_id})
    if reply.get("status") != "ok":
        console.print(f"[red]Vorschlag fehlgeschlagen:[/red] {reply.get('code')}")
        raise typer.Exit(1)
    console.print(
        f"[green]✓[/green] {reply.get('proposed', 0)} Vorschlag/Vorschläge "
        f"für {reply.get('chats', 0)} Chat(s)"
    )


@lessons_app.command("accept")
def lessons_accept(proposal_id: str = typer.Argument(..., help="Proposal id")) -> None:
    """Accept one proposal: write the file, record a version, best-effort git commit."""
    reply = _lessons_command("sentiment_lessons_accept", {"proposal_id": proposal_id})
    if reply.get("status") != "ok":
        console.print(f"[red]Annehmen fehlgeschlagen:[/red] {reply.get('code')}")
        raise typer.Exit(1)
    console.print(
        f"[green]✓[/green] Lessons v{reply.get('version')} für {reply.get('chat_id')}"
    )


@lessons_app.command("reject")
def lessons_reject(proposal_id: str = typer.Argument(..., help="Proposal id")) -> None:
    """Reject one open proposal without touching the file."""
    reply = _lessons_command("sentiment_lessons_reject", {"proposal_id": proposal_id})
    if reply.get("status") != "ok":
        console.print(f"[red]Ablehnen fehlgeschlagen:[/red] {reply.get('code')}")
        raise typer.Exit(1)
    console.print(f"[green]✓[/green] Vorschlag abgelehnt für {reply.get('chat_id')}")


@lessons_app.command("show")
def lessons_show(
    chat: str = typer.Argument(..., help="Chat id or comment name"),
    version: int | None = typer.Option(None, "--version", min=1, help="Show this version"),
) -> None:
    """Read lesson versions and open proposals; no gateway required."""
    chat_id = _lessons_chat_id(chat)
    store = _store_read_only()
    if store is None:
        console.print("Keine Empfangsdaten gespeichert (sentiment.db fehlt).")
        return
    try:
        versions = store.lesson_versions(chat_id)
        if not versions:
            console.print("Keine Lessons-Version gespeichert.")
            return
        chosen = (
            next((item for item in versions if item.version == version), None)
            if version is not None
            else versions[-1]
        )
        if chosen is None:
            console.print(f"[red]Unbekannte Version:[/red] {version}")
            raise typer.Exit(2)
        console.print(f"[bold]Lessons v{chosen.version}[/bold] ({chat_id})")
        for item in chosen.items:
            console.print(
                f"- Situation: {item.get('situation', '')}\n"
                f"  Effect: {item.get('effect', '')}\n"
                f"  Change: {item.get('change', '')}"
            )
        open_proposals = store.lesson_proposals(chat_id, status="open")
        if open_proposals:
            console.print("[bold]Offene Vorschläge[/bold]")
            for proposal in open_proposals:
                console.print(f"{proposal.id}: {proposal.diff_text}")
    finally:
        store.close()


@lessons_app.command("rollback")
def lessons_rollback(
    chat: str = typer.Argument(..., help="Chat id or comment name"),
    version: int | None = typer.Option(
        None, "--version", min=1, help="Target version; default previous version"
    ),
) -> None:
    """Write an old version's items as a new version through the gateway (owner-only)."""
    chat_id = _lessons_chat_id(chat)
    reply = _lessons_command(
        "sentiment_lessons_rollback", {"chat": chat_id, "version": version}
    )
    if reply.get("status") != "ok":
        console.print(f"[red]Rücknahme fehlgeschlagen:[/red] {reply.get('code')}")
        raise typer.Exit(1)
    console.print(
        f"[green]✓[/green] Lessons v{reply.get('version')} für {reply.get('chat_id')}"
    )



__all__ = ["sentiment_app"]
