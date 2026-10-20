"""Trends CLI: review collected cards and run one collection by hand."""

from __future__ import annotations

import asyncio
import time

import typer
from rich.table import Table

from .core import app, console

trends_app = typer.Typer(help="Review and refresh trend cards for speak-ups")
app.add_typer(trends_app, name="trends")


@trends_app.command("list")
def trends_list(
    scores: bool = typer.Option(False, "--scores", "-s", help="Show every scored topic, not only cards"),
    limit: int = typer.Option(30, "--limit", "-l", help="Maximum number of rows"),
) -> None:
    """Show active cards (or recent scores with --scores)."""
    from yeoman_shared.utils.helpers import get_operational_store_path

    from yeoman_gateway.trends.store import TrendStore

    store = TrendStore(get_operational_store_path("trends"))
    try:
        if scores:
            table = Table("score", "kind", "topic", "reason")
            for row in store.recent_scores(limit=limit):
                table.add_row(str(row["score"]), row["kind"], row["title"], row["reason"])
        else:
            table = Table("score", "kind", "topic", "line", "url")
            for card in store.active_cards(now_ms=int(time.time() * 1000), limit=limit):
                table.add_row(str(card.score), card.kind, card.title, card.line, card.url)
        console.print(table)
    finally:
        store.close()


@trends_app.command("refresh")
def trends_refresh() -> None:
    """Fetch and score new topics now, using the configured route."""
    from yeoman_shared.config.loader import load_config

    from yeoman_gateway.processing.model_route import RouteUnavailableError
    from yeoman_gateway.trends.service import build_trends_service

    try:
        service = build_trends_service(load_config())
    except RouteUnavailableError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    try:
        report = asyncio.run(service.run_once())
    finally:
        service.store.close()
    console.print(report.as_dict())
