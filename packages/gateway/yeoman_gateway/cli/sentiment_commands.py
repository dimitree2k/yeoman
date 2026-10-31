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


async def _request_sentiment_run(socket_path: Path, args: dict[str, str]) -> dict:
    writer = None
    try:
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        writer.write(json.dumps({"cmd": "sentiment_run", "args": args}).encode() + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=600)
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
        "stance_total", "stance_accuracy",
    ):
        table.add_row(key, str(report[key]))
    console.print(table)
    console.print("responds_confusion " + json.dumps(report["responds_confusion"], sort_keys=True))
    console.print("stance_confusion " + json.dumps(report["stance_confusion"], sort_keys=True))


__all__ = ["sentiment_app"]
