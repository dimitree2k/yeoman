"""Memory and notes CLI commands."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from typing import Literal

import typer
from rich.table import Table

from yeoman_gateway.memory.disclosure import (
    DISCLOSURE_MODES,
    SENSITIVITIES,
    disclosure_decision,
    normalize_list,
    normalize_metadata,
)
from yeoman_gateway.memory.disclosure_backfill import (
    ModelDisclosureClassifier,
    NarrowDisclosureClassifier,
    run_disclosure_backfill,
)

from .core import app, console, make_memory_service

memory_app = typer.Typer(help="Manage long-term memory")
app.add_typer(memory_app, name="memory")
notes_app = typer.Typer(help="Manage background group notes capture")
memory_app.add_typer(notes_app, name="notes")

MEMORY_KINDS = {"preference", "decision", "fact", "episodic"}
MEMORY_SCOPES = {"chat", "user", "global", "all"}
MEMORY_SENSITIVITIES = set(SENSITIVITIES)
MEMORY_DISCLOSURES = set(DISCLOSURE_MODES)
NOTES_CHANNELS = {"whatsapp", "telegram"}


def _normalize_choice(raw: str, *, choices: set[str], option: str) -> str:
    value = raw.strip().lower()
    if value not in choices:
        console.print(f"[red]Invalid {option}. Use: {'|'.join(sorted(choices))}[/red]")
        raise typer.Exit(1)
    return value


@contextmanager
def _memory_service_context():
    from yeoman_shared.config.loader import load_config

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


def _parse_csv(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    return list(normalize_list(raw))


def _metadata_display(meta_json: str) -> tuple[str, str]:
    metadata = normalize_metadata(meta_json)
    topics = ",".join(metadata.topics)
    return metadata.sensitivity, topics


def _resolve_chat_profile(config, profile_name: str):
    from yeoman_shared.config.loader import camel_to_snake

    from yeoman_gateway.media.router import ModelRouter

    profile = ModelRouter(config.models).resolve_by_profile(camel_to_snake(profile_name))
    if profile.kind != "chat":
        console.print(f"[red]Profile {profile_name!r} is kind={profile.kind!r}, expected chat[/red]")
        raise typer.Exit(1)
    if not profile.model:
        console.print(f"[red]Profile {profile_name!r} has no model[/red]")
        raise typer.Exit(1)
    return profile


@notes_app.command("status")
def memory_notes_status(
    channel: str = typer.Option(..., "--channel", help="Channel name"),
    chat_id: str = typer.Option(..., "--chat-id", help="Chat id"),
    is_group: bool = typer.Option(True, "--is-group/--is-dm", help="Resolve as group or DM"),
) -> None:
    """Show effective background memory-notes settings for one chat."""
    from yeoman_shared.config.loader import load_config

    from yeoman_gateway.policy.engine import PolicyEngine
    from yeoman_gateway.policy.loader import load_policy

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
    from yeoman_gateway.policy.loader import load_policy, save_policy
    from yeoman_gateway.policy.schema import MemoryNotesChannelPolicy, MemoryNotesOverride

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


@memory_app.command("taste-status")
def memory_taste_status(
    channel: str = typer.Option(..., "--channel", help="Channel for chat taste"),
    chat_id: str = typer.Option(..., "--chat-id", help="Chat id for chat taste"),
    limit: int = typer.Option(5, "--limit", "-n", min=1, max=50),
) -> None:
    """Show learned proactive speakup taste for one chat."""
    with _memory_service_context() as service:
        hits = service.learned_chat_taste(channel=channel, chat_id=chat_id, limit=limit)

    console.print("[bold]Learned Chat Taste[/bold]")
    console.print(f"channel: {channel}")
    console.print(f"chat_id: {chat_id}")
    if not hits:
        console.print("No learned proactive taste patterns.")
        return

    table = Table(title="Taste Patterns")
    table.add_column("Score", justify="right")
    table.add_column("Confidence", justify="right")
    table.add_column("Updated")
    table.add_column("Pattern")
    for hit in hits:
        content = " ".join(hit.entry.content.split())
        if len(content) > 160:
            content = content[:157] + "..."
        table.add_row(
            f"{hit.final_score:.2f}",
            f"{hit.entry.confidence:.2f}",
            hit.entry.updated_at[:19],
            content,
        )
    console.print(table)


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
    table.add_column("Kind", min_width=10, no_wrap=True)
    table.add_column("Scope")
    table.add_column("Sensitivity", min_width=11, no_wrap=True)
    table.add_column("Topics", min_width=14, no_wrap=True)
    table.add_column("Updated")
    table.add_column("Content")
    for hit in hits:
        content = " ".join(hit.entry.content.split())
        if len(content) > 120:
            content = content[:117] + "..."
        sensitivity, topics = _metadata_display(hit.entry.meta_json)
        table.add_row(
            f"{hit.final_score:.2f}",
            hit.entry.kind,
            hit.entry.scope_type,
            sensitivity,
            topics,
            hit.entry.updated_at[:19],
            content,
        )
    console.print(table)


@memory_app.command("trace")
def memory_trace(
    query: str = typer.Option(..., "--query", "-q", help="Trace query"),
    channel: str = typer.Option(..., "--channel", help="Channel for scoped trace"),
    chat_id: str = typer.Option(..., "--chat-id", help="Chat id for scoped trace"),
    sender_id: str | None = typer.Option(None, "--sender-id", help="Sender id for user scope"),
    reply_to_text: str | None = typer.Option(
        None,
        "--reply-to-text",
        help="Quoted/reply text included in recall",
    ),
    reply_to_jid: str | None = typer.Option(
        None,
        "--reply-to-jid",
        help="Quoted/reply sender jid included in recall",
    ),
) -> None:
    """Trace recall scoring, query origin, quota, and disclosure decision."""
    with _memory_service_context() as service:
        hits = service.recall_for_event(
            channel=channel,
            chat_id=chat_id,
            sender_id=sender_id,
            query=query,
            reply_to_text=reply_to_text,
            reply_to_jid=reply_to_jid,
        )
        query_text = service._normalize_content(
            query + (f"\n{reply_to_text}" if reply_to_text else "")
        )
        owner_context = service._is_owner(channel, sender_id)

    if not hits:
        console.print("No memory hits.")
        return

    table = Table(title="Memory Trace")
    table.add_column("Rank", justify="right")
    table.add_column("Score", justify="right")
    table.add_column("query_origin", min_width=12)
    table.add_column("quota", min_width=7)
    table.add_column("disclosure", min_width=11)
    table.add_column("Mode", min_width=10)
    table.add_column("Kind", min_width=10, no_wrap=True)
    table.add_column("Sensitivity", min_width=11, no_wrap=True)
    table.add_column("Topics", min_width=14, no_wrap=True)
    table.add_column("Content")
    for index, hit in enumerate(hits, start=1):
        metadata = normalize_metadata(hit.entry.meta_json)
        decision = disclosure_decision(
            metadata,
            query=query_text,
            owner_context=owner_context,
        )
        content = " ".join(hit.entry.content.split())
        if len(content) > 120:
            content = content[:117] + "..."
        table.add_row(
            str(index),
            f"{hit.final_score:.2f}",
            str(hit.trace.get("query_origin") or "-"),
            str(hit.trace.get("quota") or "-"),
            decision,
            metadata.disclosure_mode,
            hit.entry.kind,
            metadata.sensitivity,
            ",".join(metadata.topics),
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
    topics: str | None = typer.Option(None, "--topics", help="Comma-separated topic tags"),
    sensitivity: str = typer.Option("normal", "--sensitivity", help="normal|sensitive|private|taboo"),
    disclosure: str = typer.Option(
        "speakable",
        "--disclosure",
        help="speakable|context_only|owner_only|never_initiate",
    ),
    subjects: str | None = typer.Option(None, "--subjects", help="Comma-separated subject tags"),
) -> None:
    """Add one manual memory entry."""
    kind_value = _normalize_choice(kind, choices=MEMORY_KINDS, option="--kind")
    scope_value = _normalize_choice(scope, choices=MEMORY_SCOPES - {"all"}, option="--scope")
    sensitivity_value = _normalize_choice(
        sensitivity,
        choices=MEMORY_SENSITIVITIES,
        option="--sensitivity",
    )
    disclosure_value = _normalize_choice(
        disclosure,
        choices=MEMORY_DISCLOSURES,
        option="--disclosure",
    )

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
            topics=_parse_csv(topics),
            sensitivity=sensitivity_value,
            disclosure_mode=disclosure_value,
            subjects=_parse_csv(subjects),
        )

    action = "Inserted" if inserted else "Merged"
    console.print(f"[green]✓[/green] {action} memory entry: {entry.id}")
    console.print(f"scope={entry.scope_type}:{entry.scope_key}")


@memory_app.command("tag")
def memory_tag(
    entry_id: str = typer.Argument(..., help="Memory entry id to update"),
    topics: str | None = typer.Option(None, "--topics", help="Comma-separated topic tags"),
    sensitivity: str | None = typer.Option(None, "--sensitivity", help="normal|sensitive|private|taboo"),
    disclosure: str | None = typer.Option(
        None,
        "--disclosure",
        help="speakable|context_only|owner_only|never_initiate",
    ),
    subjects: str | None = typer.Option(None, "--subjects", help="Comma-separated subject tags"),
) -> None:
    """Update disclosure metadata for an existing memory entry."""
    sensitivity_value = (
        _normalize_choice(sensitivity, choices=MEMORY_SENSITIVITIES, option="--sensitivity")
        if sensitivity is not None
        else None
    )
    disclosure_value = (
        _normalize_choice(disclosure, choices=MEMORY_DISCLOSURES, option="--disclosure")
        if disclosure is not None
        else None
    )
    with _memory_service_context() as service:
        entry = service.update_disclosure_metadata(
            entry_id,
            topics=_parse_csv(topics),
            sensitivity=sensitivity_value,
            disclosure_mode=disclosure_value,
            subjects=_parse_csv(subjects),
        )
    if entry is None:
        console.print(f"[red]Memory entry not found: {entry_id}[/red]")
        raise typer.Exit(1)

    metadata = normalize_metadata(entry.meta_json)
    console.print(f"[green]✓[/green] Updated memory metadata: {entry.id}")
    console.print(f"sensitivity={metadata.sensitivity}")
    console.print(f"disclosure={metadata.disclosure_mode}")
    console.print(f"topics={','.join(metadata.topics)}")


@memory_app.command("disclosure-backfill")
def memory_disclosure_backfill(
    profile_name: str = typer.Option(
        "gptNano",
        "--profile",
        help="Chat model profile to use for classification",
    ),
    batch_size: int = typer.Option(20, "--batch-size", min=1, max=50),
    limit: int | None = typer.Option(None, "--limit", min=1, help="Limit rows for a sample run"),
    only_missing: bool = typer.Option(
        True,
        "--only-missing/--all",
        help="Classify only rows missing disclosure metadata or all active rows",
    ),
    all_workspaces: bool = typer.Option(
        False,
        "--all-workspaces",
        help="Process every workspace_id in memory.db, not only the current checkout",
    ),
    apply: bool = typer.Option(False, "--apply", help="Persist suggestions to memory.db"),
    backup: bool = typer.Option(True, "--backup/--no-backup", help="Backup memory.db before apply"),
    sample_limit: int = typer.Option(10, "--sample-limit", min=0, max=50),
) -> None:
    """Classify existing memories with disclosure metadata using a cheap model."""
    from yeoman_shared.config.loader import load_config

    from yeoman_gateway.providers.factory import ProviderFactory

    config = load_config()
    profile = _resolve_chat_profile(config, profile_name)
    provider = ProviderFactory(config=config).create_chat_provider(profile.model, profile.provider)
    classifier = ModelDisclosureClassifier(
        provider=provider,
        model=profile.model,
        max_tokens=min(int(profile.max_tokens or 4000), 6000),
        temperature=0.0,
        reasoning=profile.reasoning,
    )
    service = make_memory_service(config)
    try:
        result = asyncio.run(
            run_disclosure_backfill(
                memory=service,
                classifier=classifier,
                limit=limit,
                batch_size=batch_size,
                only_missing=only_missing,
                all_workspaces=all_workspaces,
                apply=apply,
                backup=backup,
                sample_limit=sample_limit,
            )
        )
    finally:
        service.close()

    mode = "applied" if apply else "dry-run"
    console.print("[bold]Disclosure Backfill[/bold]")
    console.print(f"mode: {mode}")
    console.print(f"profile: {profile.profile_name} ({profile.model})")
    console.print(f"scanned: {result.scanned}")
    console.print(f"suggested: {result.suggested}")
    console.print(f"applied: {result.applied}")
    console.print(f"failed_batches: {result.failed_batches}")
    if result.backup_path is not None:
        console.print(f"backup: {result.backup_path}")

    if result.samples:
        table = Table(title="Sample Suggestions")
        table.add_column("Entry")
        table.add_column("Sensitivity")
        table.add_column("Disclosure")
        table.add_column("Topics")
        table.add_column("Subjects")
        for suggestion in result.samples:
            table.add_row(
                suggestion.entry_id[:8],
                suggestion.sensitivity,
                suggestion.disclosure_mode,
                ",".join(suggestion.topics),
                ",".join(suggestion.subjects),
            )
        console.print(table)


@memory_app.command("disclosure-retag-narrow")
def memory_disclosure_retag_narrow(
    limit: int | None = typer.Option(None, "--limit", min=1, help="Limit rows for a sample run"),
    all_workspaces: bool = typer.Option(
        False,
        "--all-workspaces",
        help="Process every workspace_id in memory.db, not only the current checkout",
    ),
    apply: bool = typer.Option(False, "--apply", help="Persist deterministic retags to memory.db"),
    backup: bool = typer.Option(True, "--backup/--no-backup", help="Backup memory.db before apply"),
    sample_limit: int = typer.Option(10, "--sample-limit", min=0, max=50),
) -> None:
    """Retag existing memories with Yeoman's narrow deterministic disclosure policy."""
    from yeoman_shared.config.loader import load_config

    config = load_config()
    service = make_memory_service(config)
    try:
        result = asyncio.run(
            run_disclosure_backfill(
                memory=service,
                classifier=NarrowDisclosureClassifier(),
                limit=limit,
                batch_size=200,
                only_missing=False,
                all_workspaces=all_workspaces,
                apply=apply,
                backup=backup,
                sample_limit=sample_limit,
            )
        )
    finally:
        service.close()

    mode = "applied" if apply else "dry-run"
    console.print("[bold]Narrow Disclosure Retag[/bold]")
    console.print(f"mode: {mode}")
    console.print(f"scanned: {result.scanned}")
    console.print(f"suggested: {result.suggested}")
    console.print(f"applied: {result.applied}")
    console.print(f"failed_batches: {result.failed_batches}")
    if result.backup_path is not None:
        console.print(f"backup: {result.backup_path}")

    if result.samples:
        table = Table(title="Sample Retags")
        table.add_column("Entry")
        table.add_column("Sensitivity")
        table.add_column("Disclosure")
        table.add_column("Topics")
        table.add_column("Subjects")
        for suggestion in result.samples:
            table.add_row(
                suggestion.entry_id[:8],
                suggestion.sensitivity,
                suggestion.disclosure_mode,
                ",".join(suggestion.topics),
                ",".join(suggestion.subjects),
            )
        console.print(table)


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
