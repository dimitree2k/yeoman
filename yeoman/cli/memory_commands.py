"""Memory and notes CLI commands."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Literal

import typer
from rich.table import Table

from .core import app, console, make_memory_service

memory_app = typer.Typer(help="Manage long-term memory")
app.add_typer(memory_app, name="memory")
notes_app = typer.Typer(help="Manage background group notes capture")
memory_app.add_typer(notes_app, name="notes")

MEMORY_KINDS = {"preference", "decision", "fact", "episodic"}
MEMORY_SCOPES = {"chat", "user", "global", "all"}
NOTES_CHANNELS = {"whatsapp", "telegram"}


def _normalize_choice(raw: str, *, choices: set[str], option: str) -> str:
    value = raw.strip().lower()
    if value not in choices:
        console.print(f"[red]Invalid {option}. Use: {'|'.join(sorted(choices))}[/red]")
        raise typer.Exit(1)
    return value


@contextmanager
def _memory_service_context():
    from yeoman.config.loader import load_config

    service = make_memory_service(load_config())
    try:
        yield service
    finally:
        service.close()


def _memory_scope_keys(
    service,
    *,
    scope: str,
    channel: str | None,
    chat_id: str | None,
    sender_id: str | None,
) -> list[str]:
    keys: list[str] = []
    if scope in {"chat", "all"} and channel and chat_id:
        keys.append(service.chat_scope_key(channel, chat_id))
    if scope in {"user", "all"} and channel and (sender_id or chat_id):
        keys.append(service.user_scope_key(channel, (sender_id or chat_id or "").strip()))
    if scope in {"global", "all"}:
        keys.append(service.global_scope_key())
    return keys


def _notes_channel_guard(channel: str) -> str:
    return _normalize_choice(channel, choices=NOTES_CHANNELS, option="--channel")


def _notes_parse_optional_bool(raw: str) -> bool | None:
    value = _normalize_choice(raw, choices={"inherit", "on", "off"}, option="value")
    mapping = {"inherit": None, "on": True, "off": False}
    return mapping[value]


def _notes_parse_optional_mode(raw: str) -> Literal["adaptive", "heuristic", "hybrid"] | None:
    value = _normalize_choice(
        raw,
        choices={"adaptive", "heuristic", "hybrid", "inherit"},
        option="value",
    )
    return None if value == "inherit" else value


@notes_app.command("status")
def memory_notes_status(
    channel: str = typer.Option(..., "--channel", help="Channel name"),
    chat_id: str = typer.Option(..., "--chat-id", help="Chat id"),
    is_group: bool = typer.Option(True, "--is-group/--is-dm", help="Resolve as group or DM"),
) -> None:
    """Show effective background memory-notes settings for one chat."""
    from yeoman.config.loader import load_config
    from yeoman.policy.engine import PolicyEngine
    from yeoman.policy.loader import load_policy

    resolved_channel = _notes_channel_guard(channel)
    config = load_config()
    policy = load_policy()
    engine = PolicyEngine(
        policy=policy,
        workspace=config.workspace_path,
        apply_channels={"telegram", "whatsapp"},
    )
    resolved = engine.resolve_memory_notes(
        channel=resolved_channel,
        chat_id=chat_id,
        is_group=is_group,
    )
    console.print("[bold]Memory Notes Status[/bold]")
    console.print(f"channel: {resolved_channel}")
    console.print(f"chat_id: {chat_id}")
    console.print(f"is_group: {is_group}")
    console.print(f"enabled: {resolved.enabled}")
    console.print(f"mode: {resolved.mode}")
    console.print(f"allow_blocked_senders: {resolved.allow_blocked_senders}")
    console.print(f"batch_interval_seconds: {resolved.batch_interval_seconds}")
    console.print(f"batch_max_messages: {resolved.batch_max_messages}")
    source_table = Table(title="Resolution Source")
    source_table.add_column("Field")
    source_table.add_column("Source")
    for key in ("enabled", "mode", "allowBlockedSenders"):
        source_table.add_row(key, str(resolved.source.get(key, "-")))
    console.print(source_table)


@notes_app.command("set")
def memory_notes_set(
    channel: str = typer.Option(..., "--channel", help="Channel name"),
    chat_id: str = typer.Option(..., "--chat-id", help="Chat id"),
    enabled: str = typer.Option("inherit", "--enabled", help="on|off|inherit"),
    mode: str = typer.Option("inherit", "--mode", help="adaptive|hybrid|heuristic|inherit"),
    allow_blocked: str = typer.Option(
        "inherit",
        "--allow-blocked",
        help="on|off|inherit",
    ),
) -> None:
    """Set per-chat memory-notes override in policy.json."""
    from yeoman.policy.loader import load_policy, save_policy
    from yeoman.policy.schema import MemoryNotesChannelPolicy, MemoryNotesOverride

    resolved_channel = _notes_channel_guard(channel)
    enabled_value = _notes_parse_optional_bool(enabled)
    mode_value = _notes_parse_optional_mode(mode)
    allow_blocked_value = _notes_parse_optional_bool(allow_blocked)

    policy = load_policy()
    channel_cfg = policy.memory_notes.channels.get(resolved_channel)
    if channel_cfg is None:
        channel_cfg = MemoryNotesChannelPolicy()
        policy.memory_notes.channels[resolved_channel] = channel_cfg

    override = channel_cfg.chats.get(chat_id)
    if override is None:
        override = MemoryNotesOverride()
        channel_cfg.chats[chat_id] = override

    override.enabled = enabled_value
    override.mode = mode_value
    override.allow_blocked_senders = allow_blocked_value

    if (
        override.enabled is None
        and override.mode is None
        and override.allow_blocked_senders is None
    ):
        channel_cfg.chats.pop(chat_id, None)

    save_policy(policy)
    console.print("[green]✓[/green] Updated memory notes policy override.")
    console.print(f"channel={resolved_channel} chat_id={chat_id}")


@memory_app.command("status")
def memory_status() -> None:
    """Show long-term memory status and counters."""
    with _memory_service_context() as service:
        stats = service.stats()

    console.print("[bold]Memory Status[/bold]")
    console.print(f"enabled: {stats.get('enabled')}")
    console.print(f"backend: {stats.get('backend')}")
    console.print(f"wal_enabled: {stats.get('wal_enabled')}")
    console.print(f"db_path: {stats.get('db_path')}")
    console.print(f"state_dir: {stats.get('state_dir')}")
    console.print(f"total_active: {stats.get('total_active')}")
    console.print(f"total_deleted: {stats.get('total_deleted')}")
    console.print(f"wal_files: {stats.get('wal_files')}")
    marker = str(stats.get("backfill_marker") or "")
    console.print(f"backfill_marker: {marker or '(not set)'}")

    kind_table = Table(title="By Kind")
    kind_table.add_column("Kind")
    kind_table.add_column("Count", justify="right")
    for kind, count in sorted((stats.get("by_kind") or {}).items()):
        kind_table.add_row(str(kind), str(count))
    console.print(kind_table)

    scope_table = Table(title="By Scope")
    scope_table.add_column("Scope")
    scope_table.add_column("Count", justify="right")
    for scope_name, count in sorted((stats.get("by_scope") or {}).items()):
        scope_table.add_row(str(scope_name), str(count))
    console.print(scope_table)


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

    with _memory_service_context() as service:
        hits = service.search(
            query=query,
            channel=channel,
            chat_id=chat_id,
            sender_id=sender_id,
            scope=scope_value,
            limit=limit,
        )

    if not hits:
        console.print("No memory hits.")
        return

    table = Table(title="Memory Search Results")
    table.add_column("Score", justify="right")
    table.add_column("Kind")
    table.add_column("Scope")
    table.add_column("Updated")
    table.add_column("Content")
    for hit in hits:
        content = " ".join(hit.entry.content.split())
        if len(content) > 120:
            content = content[:117] + "..."
        table.add_row(
            f"{hit.final_score:.2f}",
            hit.entry.kind,
            hit.entry.scope_type,
            hit.entry.updated_at[:19],
            content,
        )
    console.print(table)


@memory_app.command("add")
def memory_add(
    text: str = typer.Option(..., "--text", "-t", help="Memory text"),
    kind: str = typer.Option(..., "--kind", "-k", help="preference|decision|fact|episodic"),
    scope: str = typer.Option("chat", "--scope", help="chat|user|global"),
    channel: str = typer.Option("cli", "--channel", help="Channel for chat/user scope"),
    chat_id: str = typer.Option("direct", "--chat-id", help="Chat id for chat/user scope"),
    sender_id: str | None = typer.Option(None, "--sender-id", help="Sender id for user scope"),
    importance: float = typer.Option(0.8, "--importance", min=0.0, max=1.0),
    confidence: float = typer.Option(1.0, "--confidence", min=0.0, max=1.0),
) -> None:
    """Add one manual memory entry."""
    kind_value = _normalize_choice(kind, choices=MEMORY_KINDS, option="--kind")
    scope_value = _normalize_choice(scope, choices=MEMORY_SCOPES - {"all"}, option="--scope")

    with _memory_service_context() as service:
        entry, inserted = service.record_manual(
            channel=channel,
            chat_id=chat_id,
            sender_id=sender_id,
            scope_type=scope_value,
            kind=kind_value,
            text=text,
            importance=importance,
            confidence=confidence,
        )

    action = "Inserted" if inserted else "Merged"
    console.print(f"[green]✓[/green] {action} memory entry: {entry.id}")
    console.print(f"scope={entry.scope_type}:{entry.scope_key}")


@memory_app.command("prune")
def memory_prune(
    older_than_days: int | None = typer.Option(
        None,
        "--older-than-days",
        help="Prune entries older than N days by updated_at",
    ),
    kind: str | None = typer.Option(None, "--kind", help="Optional kind filter"),
    scope: str = typer.Option("all", "--scope", help="chat|user|global|all"),
    channel: str | None = typer.Option(None, "--channel", help="Channel for scope filter"),
    chat_id: str | None = typer.Option(None, "--chat-id", help="Chat id for scope filter"),
    sender_id: str | None = typer.Option(None, "--sender-id", help="Sender id for user scope"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview only"),
) -> None:
    """Prune long-term memory entries safely."""
    scope_value = _normalize_choice(scope, choices=MEMORY_SCOPES, option="--scope")

    kinds: set[str] | None = None
    if kind:
        kinds = {_normalize_choice(kind, choices=MEMORY_KINDS, option="--kind")}

    with _memory_service_context() as service:
        scope_keys = _memory_scope_keys(
            service,
            scope=scope_value,
            channel=channel,
            chat_id=chat_id,
            sender_id=sender_id,
        )
        pruned = service.prune(
            older_than_days=older_than_days,
            kinds=kinds,
            scope_keys=scope_keys or None,
            dry_run=dry_run,
        )

    if dry_run:
        console.print(f"[yellow]Dry run:[/yellow] {pruned} entries would be pruned.")
    else:
        console.print(f"[green]✓[/green] Pruned {pruned} entries.")


@memory_app.command("backfill")
def memory_backfill(
    force: bool = typer.Option(False, "--force", help="Run backfill even if marker exists"),
) -> None:
    """Backfill legacy memory files into long-term memory DB."""
    with _memory_service_context() as service:
        imported = service.backfill_from_workspace_files(force=force)

    console.print(f"[green]✓[/green] Backfill imported {imported} entries.")


@memory_app.command("reindex")
def memory_reindex() -> None:
    """Rebuild memory full-text index."""
    with _memory_service_context() as service:
        service.reindex()

    console.print("[green]✓[/green] Memory FTS index rebuilt.")
