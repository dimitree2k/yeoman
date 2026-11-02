"""CLI for the shadow group state: inspect, diff and purge one chat."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import typer

from .core import app, console

group_state_app = typer.Typer(help="Per-chat group state (shadow)")
app.add_typer(group_state_app, name="group-state")

_SECTION_ORDER = (
    "presence",
    "arvid_actions",
    "conversations",
    "commitments",
    "budgets",
    "people_activity",
    "past",
)


def _default_db_path() -> Path:
    from yeoman_shared.config.loader import load_config
    from yeoman_shared.utils.helpers import get_data_path

    settings = load_config().processing.group_state
    path = Path(str(settings.db_path)).expanduser()
    return path if path.is_absolute() else get_data_path() / path


def _resolve_db(value: Path | None) -> Path:
    return Path(value).expanduser() if value is not None else _default_db_path()


def _chat_subjects(history_db: Path | None) -> dict[str, str]:
    """Latest group subject per chat, read-only; no message text is selected."""
    from yeoman_gateway.history.live import history_db_path

    path = Path(history_db).expanduser() if history_db is not None else history_db_path()
    if not path.is_file():
        return {}
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        try:
            rows = connection.execute(
                "SELECT chat_id, json_extract(payload_json, '$.value') AS subject"
                " FROM message_events WHERE channel='whatsapp' AND kind='group_subject'"
                " ORDER BY occurred_ms DESC, event_id DESC"
            ).fetchall()
        finally:
            connection.close()
    except OSError, sqlite3.Error, ValueError:
        return {}
    subjects: dict[str, str] = {}
    for chat_id, subject in rows:
        if subject and str(chat_id) not in subjects:
            subjects[str(chat_id)] = str(subject)
    return subjects


def _resolve_chat(chat: str, history_db: Path | None) -> str:
    from yeoman_gateway.evaluation.participation_context_ab import resolve_chat

    try:
        return resolve_chat(chat, _chat_subjects(history_db))
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None


def _format_age(age_ms: int) -> str:
    seconds = max(0, int(age_ms)) // 1000
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


def _section_payload(section: Any, now_ms: int) -> dict[str, Any]:
    return {
        "key": section.key,
        "schema_version": section.schema_version,
        "trust": section.trust,
        "layer": section.layer,
        "produced_at_ms": section.produced_at_ms,
        "age_ms": max(0, now_ms - section.produced_at_ms),
        "ttl_ms": section.ttl_ms,
        "source_cursor": section.source_cursor,
        "period_start_ms": section.period_start_ms,
        "period_end_ms": section.period_end_ms,
        "sources": list(section.sources),
        "data": section.data,
    }


def _has_activity(state: Any) -> bool:
    if not state.sections:
        return False
    if any(section.sources for section in state.sections.values()):
        return True
    presence = state.sections.get("presence")
    return bool(presence is not None and presence.data.get("last_message_ms") is not None)


def _section_order(state: Any) -> list[str]:
    keys = list(state.sections)
    ordered = [key for key in _SECTION_ORDER if key in state.sections]
    return ordered + [key for key in keys if key not in ordered]


@group_state_app.command("show")
def group_state_show(
    chat: str = typer.Option(..., "--chat", help="Chat id or exact group subject"),
    db_path: Path | None = typer.Option(None, "--db-path", help="Group-state database"),
    history_db: Path | None = typer.Option(None, "--history-db", help="History database"),
    channel: str = typer.Option("whatsapp", "--channel", help="Channel"),
    json_output: bool = typer.Option(False, "--json", help="Output JSON"),
) -> None:
    """Show one chat's state: header and one block per section."""
    import time

    from yeoman_gateway.group_state.store import GroupStateStore

    path = _resolve_db(db_path)
    if not path.is_file():
        typer.echo("No group state yet.", err=True)
        raise typer.Exit(1)
    chat_id = _resolve_chat(chat, history_db)
    store = GroupStateStore(path, readonly=True)
    try:
        state = store.get(channel, chat_id)
    finally:
        store.close()
    if state is None:
        typer.echo(f"No group state for {channel}:{chat_id}.", err=True)
        raise typer.Exit(1)
    now_ms = int(time.time() * 1000)
    age_ms = state.age_ms(now_ms)
    if json_output:
        payload = {
            "channel": state.channel,
            "chat_id": state.chat_id,
            "schema_version": state.schema_version,
            "revision": state.revision,
            "history_cursor": state.history_cursor,
            "updated_ms": state.updated_ms,
            "age_ms": age_ms,
            "sections": {
                key: _section_payload(state.sections[key], now_ms) for key in _section_order(state)
            },
        }
        typer.echo(json.dumps(payload, sort_keys=True, ensure_ascii=False))
        return
    typer.echo(f"chat: {state.channel}:{state.chat_id}")
    typer.echo(f"revision: {state.revision}")
    typer.echo(f"age: {_format_age(age_ms)}")
    typer.echo(f"history cursor: {state.history_cursor or '-'}")
    if not _has_activity(state):
        typer.echo("no activity")
        return
    for key in _section_order(state):
        section = state.sections[key]
        typer.echo(
            f"\n{key} [layer={section.layer}, trust={section.trust},"
            f" age={_format_age(max(0, now_ms - section.produced_at_ms))}]"
        )
        console.print_json(json.dumps(section.data, sort_keys=True, ensure_ascii=False))


@group_state_app.command("diff")
def group_state_diff(
    chat: str = typer.Option(..., "--chat", help="Chat id or exact group subject"),
    revisions: int = typer.Option(2, "--revisions", min=2, help="Snapshots to compare"),
    db_path: Path | None = typer.Option(None, "--db-path", help="Group-state database"),
    history_db: Path | None = typer.Option(None, "--history-db", help="History database"),
    channel: str = typer.Option("whatsapp", "--channel", help="Channel"),
    json_output: bool = typer.Option(False, "--json", help="Output JSON"),
) -> None:
    """Section-level differences between the newest snapshots."""
    from yeoman_gateway.group_state.store import GroupStateStore

    path = _resolve_db(db_path)
    if not path.is_file():
        typer.echo("No group state yet.", err=True)
        raise typer.Exit(1)
    chat_id = _resolve_chat(chat, history_db)
    store = GroupStateStore(path, readonly=True)
    try:
        snapshots = store.snapshots(channel, chat_id, limit=int(revisions))
    finally:
        store.close()
    if len(snapshots) < int(revisions):
        typer.echo(
            f"Only {len(snapshots)} snapshot(s) for {channel}:{chat_id}; asked for {revisions}.",
            err=True,
        )
        raise typer.Exit(1)
    newest, oldest = snapshots[0], snapshots[-1]
    changes: dict[str, str] = {}
    for key in _section_order(oldest) + [
        key for key in _section_order(newest) if key not in oldest.sections
    ]:
        if key not in oldest.sections:
            changes[key] = "added"
        elif key not in newest.sections:
            changes[key] = "removed"
        elif (
            oldest.sections[key].data != newest.sections[key].data
            or oldest.sections[key].sources != newest.sections[key].sources
            or oldest.sections[key].source_cursor != newest.sections[key].source_cursor
        ):
            changes[key] = "changed"
        else:
            changes[key] = "unchanged"
    if json_output:
        typer.echo(
            json.dumps(
                {
                    "channel": channel,
                    "chat_id": chat_id,
                    "from_revision": oldest.revision,
                    "to_revision": newest.revision,
                    "sections": changes,
                },
                sort_keys=True,
            )
        )
        return
    typer.echo(f"chat: {channel}:{chat_id}")
    typer.echo(f"revision {oldest.revision} -> {newest.revision}")
    markers = {"added": "+", "removed": "-", "changed": "~", "unchanged": "="}
    for key, change in changes.items():
        typer.echo(f"{markers[change]} {key}")


@group_state_app.command("purge")
def group_state_purge(
    chat: str = typer.Option(..., "--chat", help="Chat id or exact group subject"),
    yes: bool = typer.Option(False, "--yes", help="Confirm deletion"),
    db_path: Path | None = typer.Option(None, "--db-path", help="Group-state database"),
    history_db: Path | None = typer.Option(None, "--history-db", help="History database"),
    channel: str = typer.Option("whatsapp", "--channel", help="Channel"),
) -> None:
    """Remove one chat's state and snapshots."""
    from yeoman_gateway.group_state.store import GroupStateStore

    if not yes:
        typer.echo("Refusing to purge without --yes.", err=True)
        raise typer.Exit(1)
    path = _resolve_db(db_path)
    chat_id = _resolve_chat(chat, history_db)
    if not path.is_file():
        typer.echo("purged 0 row(s)")
        return
    store = GroupStateStore(path)
    try:
        removed = store.purge_chat(channel, chat_id)
    finally:
        store.close()
    typer.echo(f"purged {removed} row(s)")


@group_state_app.command("label")
def group_state_label(
    chat: str | None = typer.Argument(None, help="Chat id or exact group subject"),
    chat_option: str | None = typer.Option(
        None, "--chat", help="Chat id or exact group subject (option form)"
    ),
    conversation: str | None = typer.Option(None, "--conversation", help="Conversation id"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the validated labels and write nothing"
    ),
    db_path: Path | None = typer.Option(None, "--db-path", help="Group-state database"),
    history_db: Path | None = typer.Option(None, "--history-db", help="History database"),
    channel: str = typer.Option("whatsapp", "--channel", help="Channel"),
) -> None:
    """Run one conversation-label pass now, for owner review or a dry run."""
    import asyncio
    import time

    from yeoman_shared.config.loader import load_config

    from yeoman_gateway.evaluation.participation_context_ab import FrozenHistory
    from yeoman_gateway.group_state.labels.runner import ConversationLabeler
    from yeoman_gateway.group_state.store import GroupStateStore
    from yeoman_gateway.history.context import history_turn
    from yeoman_gateway.history.conversations import ConversationBounds, read_conversations
    from yeoman_gateway.history.live import history_db_path
    from yeoman_gateway.history.queries import HistoryQueries
    from yeoman_gateway.processing.model_route import RouteClient

    target = str(chat_option or chat or "").strip()
    if not target:
        typer.echo("Pass a chat id or a group subject.", err=True)
        raise typer.Exit(2)
    config = load_config()
    group_state = config.processing.group_state
    labels = group_state.labels
    history_path = Path(history_db).expanduser() if history_db is not None else history_db_path()
    if not history_path.is_file():
        typer.echo("No history yet.", err=True)
        raise typer.Exit(1)
    chat_id = _resolve_chat(target, history_path)
    now_ms = int(time.time() * 1000)
    consciousness = config.consciousness
    bounds = ConversationBounds(
        window_ms=max(1, int(group_state.window_hours)) * 3_600_000,
        max_rows=500,
        lull_ms=max(1, int(getattr(consciousness, "lull_silence_minutes", 25))) * 60_000,
        burst_n=max(1, int(getattr(consciousness, "burst_threshold_messages", 8))),
        burst_ms=max(1, int(getattr(consciousness, "burst_window_minutes", 15))) * 60_000,
    )
    history = FrozenHistory(history_path)
    store = None
    try:

        async def _read() -> list[Any]:
            async with history_turn(history) as snapshot:
                return read_conversations(HistoryQueries(snapshot), chat_id, now_ms, bounds)

        conversations = asyncio.run(_read())
        if conversation:
            selected = next((item for item in conversations if item.id == conversation), None)
        else:
            selected = conversations[-1] if conversations else None
        if selected is None:
            typer.echo("No conversation to label.", err=True)
            raise typer.Exit(1)
        mapping = {
            "conversation_id": selected.id,
            "start_ms": selected.start_ms,
            "last_ms": selected.last_ms,
            "messages": selected.message_count,
            "last_message_id": selected.message_ids[-1],
        }
        if not dry_run:
            store = GroupStateStore(_resolve_db(db_path))
        labeler = ConversationLabeler(
            store=store,
            history_projector=history,
            settings=labels,
            client_factory=lambda: RouteClient(config=config, route_key=labels.route),
        )
        if dry_run:
            outcome, parsed = asyncio.run(labeler.preview(channel, chat_id, mapping))
            payload: dict[str, Any] = {
                "outcome": outcome.outcome,
                "attempts": outcome.attempts,
                "prompt_tokens": outcome.prompt_tokens,
                "completion_tokens": outcome.completion_tokens,
                "labels": (
                    {
                        "topic": parsed.topic,
                        "mood": parsed.mood,
                        "open_questions": list(parsed.open_questions),
                        "plans": list(parsed.plans),
                        "dropped": parsed.dropped,
                    }
                    if parsed is not None
                    else None
                ),
            }
            typer.echo(json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2))
        else:
            outcome = asyncio.run(labeler.label(channel, chat_id, mapping))
            typer.echo(
                f"outcome={outcome.outcome} items={outcome.items}"
                f" attempts={outcome.attempts}"
                f" tokens={outcome.prompt_tokens + outcome.completion_tokens}"
            )
        if outcome.outcome in {"unavailable", "error"}:
            raise typer.Exit(1)
    finally:
        if store is not None:
            store.close()
        history.close()


@group_state_app.command("summaries")
def group_state_summaries(
    chat: str | None = typer.Argument(None, help="Chat id or exact group subject"),
    chat_option: str | None = typer.Option(
        None, "--chat", help="Chat id or exact group subject (option form)"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print counts and a token estimate; make no call, write nothing"
    ),
    db_path: Path | None = typer.Option(None, "--db-path", help="Group-state database"),
    history_db: Path | None = typer.Option(None, "--history-db", help="History database"),
    channel: str = typer.Option("whatsapp", "--channel", help="Channel"),
) -> None:
    """Build missing ended-conversation and closed-week summaries now."""
    import asyncio
    import time

    from yeoman_shared.config.loader import load_config

    from yeoman_gateway.evaluation.participation_context_ab import FrozenHistory
    from yeoman_gateway.group_state.store import GroupStateStore
    from yeoman_gateway.group_state.summaries.runner import ConversationSummarizer
    from yeoman_gateway.history.live import history_db_path
    from yeoman_gateway.processing.model_route import RouteClient

    target = str(chat_option or chat or "").strip()
    if not target:
        typer.echo("Pass a chat id or a group subject.", err=True)
        raise typer.Exit(2)
    config = load_config()
    group_state = config.processing.group_state
    summaries = group_state.summaries
    history_path = Path(history_db).expanduser() if history_db is not None else history_db_path()
    if not history_path.is_file():
        typer.echo("No history yet.", err=True)
        raise typer.Exit(1)
    chat_id = _resolve_chat(target, history_path)
    now_ms = int(time.time() * 1000)
    history = FrozenHistory(history_path)
    store = None
    try:
        path = _resolve_db(db_path)
        if dry_run:
            if path.is_file():
                store = GroupStateStore(path, readonly=True)
        else:
            store = GroupStateStore(path)
        summarizer = ConversationSummarizer(
            store=store,
            history_projector=history,
            settings=summaries,
            group_state_settings=group_state,
            consciousness=config.consciousness,
            client_factory=lambda: RouteClient(config=config, route_key=summaries.route),
        )
        if dry_run:
            preview = asyncio.run(summarizer.preview(channel, chat_id, now_ms=now_ms))
            typer.echo(
                json.dumps(
                    {
                        "mode": summaries.mode,
                        "conversations": preview.conversations,
                        "weeks": preview.weeks,
                        "estimated_prompt_tokens": preview.estimated_prompt_tokens,
                    },
                    sort_keys=True,
                )
            )
            return
        outcome = asyncio.run(summarizer.run(channel, chat_id, now_ms=now_ms))
        typer.echo(
            f"outcome={outcome.outcome} conversations={outcome.conversation_summaries}"
            f" weeks={outcome.week_summaries} attempts={outcome.attempts}"
            f" tokens={outcome.prompt_tokens + outcome.completion_tokens}"
        )
        if outcome.outcome in {"unavailable", "error"}:
            raise typer.Exit(1)
    finally:
        if store is not None:
            store.close()
        history.close()
