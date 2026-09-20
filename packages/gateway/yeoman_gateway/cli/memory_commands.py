"""Memory and notes CLI commands."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import typer
from rich.table import Table

from yeoman_gateway.knowledge._memory.disclosure import (
    DISCLOSURE_MODES,
    SENSITIVITIES,
    disclosure_decision,
    normalize_list,
    normalize_metadata,
)
from yeoman_gateway.knowledge._memory.disclosure_backfill import (
    ModelDisclosureClassifier,
    NarrowDisclosureClassifier,
    run_disclosure_backfill,
)

from .core import app, console, make_memory_service

memory_app = typer.Typer(help="Manage long-term memory")
app.add_typer(memory_app, name="memory")
notes_app = typer.Typer(help="Manage background group notes capture")
facts_app = typer.Typer(help="Inspect and correct shared chat facts (admin only)")
memory_app.add_typer(facts_app, name="facts")
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
    # The scope-key layout lives inside the knowledge module; the CLI asks for the keys
    # of a maintenance filter instead of building them itself.  A service that predates
    # the knowledge facade keeps its own (legacy) key helpers.
    builder = getattr(service, "maintenance_scope_keys", None)
    if builder is not None:
        return list(
            builder(scope=scope, channel=channel, chat_id=chat_id, sender_id=sender_id)
        )
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
    from yeoman_gateway.knowledge.api import import_lineage, inspect_lineage_sources

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


@memory_app.command("reindex")
def memory_reindex() -> None:
    """Rebuild memory full-text index."""
    with _memory_service_context() as service:
        service.reindex()

    console.print("[green]✓[/green] Memory FTS index rebuilt.")


@facts_app.command("list")
def memory_facts_list(
    chat: str | None = typer.Option(None, "--chat", help="Filter by chat scope key"),
    include_inactive: bool = typer.Option(
        True, "--include-inactive/--active-only", help="Include revoked and superseded facts"
    ),
    limit: int = typer.Option(50, "--limit", min=1, max=500),
) -> None:
    """List shared facts as metadata: no raw text of other principals is printed."""
    with _memory_service_context() as service:
        facts = service.store.list_facts(
            workspace_id=service.workspace_id,
            chat_scope_key=chat,
            include_inactive=include_inactive,
            limit=limit,
        )

    table = Table(title="Shared facts")
    for column in ("fact_id", "status", "scope", "audience", "sources", "valid_until_ms"):
        table.add_column(column)
    for fact in facts:
        table.add_row(
            fact.fact_id,
            fact.assertion_status,
            fact.visibility_scope,
            str(len(fact.audience)),
            str(len(fact.sources)),
            "-" if fact.valid_until_ms is None else str(fact.valid_until_ms),
        )
    console.print(table)
    console.print(f"total: {len(facts)}")


@facts_app.command("show")
def memory_facts_show(
    fact_id: str = typer.Argument(..., help="Fact id"),
    content: bool = typer.Option(
        False, "--content", help="Also print the stored text (admin diagnostics)"
    ),
) -> None:
    """Show one fact's metadata, its sources and (optionally) its text."""
    with _memory_service_context() as service:
        fact = service.store.get_fact(fact_id)

    if fact is None:
        console.print(f"fact not found: {fact_id}")
        raise typer.Exit(code=1)
    console.print(f"fact_id: {fact.fact_id}")
    console.print(f"status: {fact.assertion_status}")
    console.print(f"visibility: {fact.visibility_scope} / rule={fact.group_rule}")
    console.print(f"chat: {fact.chat_scope_key}")
    console.print(f"author: {fact.author_principal}")
    console.print(f"audience: {len(fact.audience)} principal(s)")
    console.print(f"revoked_at_ms: {fact.revoked_at_ms}")
    console.print(f"superseded_by: {fact.superseded_by or '-'}")
    if content:
        console.print(f"content: {fact.content}")
    for source in fact.sources:
        console.print(f"source: {source.source_event_id} rev={source.source_revision}")


@facts_app.command("revoke")
def memory_facts_revoke(
    fact_ids: list[str] = typer.Argument(..., help="Fact ids to revoke"),
    now_ms: int | None = typer.Option(None, "--now-ms", help="Override the clock (tests)"),
) -> None:
    """Revoke facts by hand: tombstone, cleared text and a bumped acl_epoch."""
    import time

    stamp = int(now_ms) if now_ms is not None else int(time.time() * 1000)
    with _memory_service_context() as service:
        for fact_id in fact_ids:
            changed = service.store.redact_fact(fact_id, now_ms=stamp)
            console.print(f"{fact_id}: {'revoked' if changed else 'not found'}")


@facts_app.command("supersede")
def memory_facts_supersede(
    fact_id: str = typer.Argument(..., help="Fact id to supersede"),
    by: str = typer.Option(..., "--by", help="Replacement reference (fact id or note)"),
    now_ms: int | None = typer.Option(None, "--now-ms", help="Override the clock (tests)"),
) -> None:
    """Supersede a fact: the older revision becomes unreadable, the tombstone points on."""
    import time

    stamp = int(now_ms) if now_ms is not None else int(time.time() * 1000)
    with _memory_service_context() as service:
        changed = service.store.set_fact_status(
            fact_id, status="superseded", now_ms=stamp, superseded_by=by
        )
    console.print(f"{fact_id}: {'superseded' if changed else 'not found'} -> {by}")


@facts_app.command("jobs")
def memory_facts_jobs(
    state: str | None = typer.Option(None, "--state", help="Filter by job state"),
    limit: int = typer.Option(20, "--limit", min=1, max=500),
) -> None:
    """Show extraction job state: what ran, what was skipped and why."""
    with _memory_service_context() as service:
        jobs = service.store.list_fact_jobs(state=state, limit=limit)
        waiting = service.store.count_fact_jobs(state="queued")

    table = Table(title="Shared fact extraction jobs")
    for column in ("job_key", "state", "reason", "attempts", "due_ms"):
        table.add_column(column)
    for job in jobs:
        table.add_row(
            str(job["job_key"])[:12],
            str(job["state"]),
            str(job.get("reason") or "-"),
            str(job.get("attempts")),
            str(job.get("due_ms")),
        )
    console.print(table)
    console.print(f"queued: {waiting}")


@facts_app.command("backfill")
def memory_facts_backfill(
    chat: str = typer.Option(..., "--chat", help="Chat id to backfill"),
    channel: str = typer.Option("whatsapp", "--channel"),
    since: str = typer.Option(
        ..., "--since", help="ISO date/time, e.g. 2026-08-11 or 2026-08-11T09:00:00"
    ),
    batch_size: int = typer.Option(20, "--batch-size", min=1, max=100),
    max_batches: int = typer.Option(20, "--max-batches", min=1),
    max_messages: int = typer.Option(300, "--max-messages", min=1),
    apply: bool = typer.Option(
        False, "--apply/--dry-run", help="Dry run by default: it plans and estimates only"
    ),
) -> None:
    """Extract shared facts from archived history. One model call per batch.

    Facts from history reference ``archive:<message_id>`` as their source, so they stay
    distinguishable from facts derived from live turns.
    """
    import time
    from datetime import UTC, datetime

    from yeoman_shared.config.loader import load_config

    from yeoman_gateway.knowledge._memory.archive_backfill import (
        ArchiveEventSource,
        run_archive_backfill,
    )
    from yeoman_gateway.knowledge._memory.extraction_jobs import (
        EXTRACTOR_VERSION,
        SharedFactExtractionQueue,
    )
    from yeoman_gateway.knowledge._memory.fact_extractor import SharedFactExtractor
    from yeoman_gateway.storage.chat_registry import ChatRegistry
    from yeoman_gateway.storage.inbound_archive import InboundArchive

    config = load_config()
    try:
        parsed = datetime.fromisoformat(since)
    except ValueError:
        console.print(f"[red]invalid --since value:[/red] {since}")
        raise typer.Exit(code=2) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    since_ms = int(parsed.timestamp() * 1000)

    registry = ChatRegistry()
    archive = InboundArchive(retention_days=None)
    with _memory_service_context() as service:
        try:
            extractor = SharedFactExtractor(
                config=config,
                route_key=config.memory.capture.extract_route,
                member_provider=_registry_member_lookup(registry),
                # The backfill resolves the same relative dates as the live path; without
                # the configured zone a re-extraction would reintroduce the UTC bug it is
                # meant to repair (F12).
                timezone_name=str(
                    getattr(config.processing.extraction, "timezone", "UTC") or "UTC"
                ),
            )
        except Exception as exc:
            console.print(f"[red]extractor unavailable:[/red] {exc}")
            raise typer.Exit(code=1) from None
        source = ArchiveEventSource(archive)
        # Mirror the live runtime: the backfill writes facts the running gateway has to
        # keep reading, so queue version and lifetime must be the configured ones, not the
        # class defaults. A different version also gives every batch a fresh job key, which
        # is what makes re-extraction over already-processed history possible at all.
        shared_cfg = getattr(config.memory, "shared", None)
        retention_cfg = getattr(config.processing, "retention", None)
        queue = SharedFactExtractionQueue(
            store=service.store,
            journal=source,
            extractor=extractor,
            embedder=service.embedding,
            max_waiting=int(getattr(shared_cfg, "max_jobs_waiting", 64)),
            fact_ttl_ms=(
                int(retention_cfg.shared_fact_days) * 24 * 3600 * 1000
                if retention_cfg
                else None
            ),
            extractor_version=str(
                getattr(shared_cfg, "extractor_version", EXTRACTOR_VERSION)
            ),
            clock=lambda: int(time.time() * 1000),
        )
        report = run_archive_backfill(
            archive=archive,
            queue=queue,
            channel=channel,
            chat_id=chat,
            workspace_id=service.workspace_id,
            since_ms=since_ms,
            batch_size=batch_size,
            max_batches=max_batches,
            max_messages=max_messages,
            dry_run=not apply,
        )
    archive.close()
    registry.close()

    console.print("[bold]Shared fact backfill[/bold]")
    for line in report.as_lines():
        console.print(line)
    if report.dry_run:
        console.print("re-run with --apply to execute")


def _registry_member_lookup(registry):
    """Proven participants from the chat registry, or ``None`` when unproven."""
    def _lookup(channel: str, chat_id: str):
        record = registry.get_chat(channel, chat_id)
        if not isinstance(record, dict):
            return None
        meta = record.get("metadata")
        participants = meta.get("participants") if isinstance(meta, dict) else None
        if not isinstance(participants, list) or not participants:
            return None
        members = set()
        for item in participants:
            if isinstance(item, str):
                members.add(item)
            elif isinstance(item, dict):
                for key in ("id", "jid", "lid", "phoneNumber", "user_id"):
                    if item.get(key):
                        members.add(str(item[key]))
                        break
        return frozenset(members) if members else None

    return _lookup


@facts_app.command("rescreen")
def memory_facts_rescreen(
    chat: str | None = typer.Option(None, "--chat", help="Limit to one chat scope key"),
    apply: bool = typer.Option(
        False, "--apply/--dry-run", help="Dry run by default: it reports only"
    ),
) -> None:
    """Apply the current content screens to stored facts and revoke the ones that fail.

    Tightening a rule must be able to clean up after itself; without this the rule would
    only ever affect new candidates.
    """
    import time

    from yeoman_gateway.knowledge._memory.extraction_jobs import rescreen_stored_facts

    stamp = int(time.time() * 1000)
    with _memory_service_context() as service:
        report = rescreen_stored_facts(
            service.store, chat_scope_key=chat, dry_run=not apply, now_ms=stamp
        )

    console.print("[bold]Shared fact re-screen[/bold]")
    for line in report.as_lines():
        console.print(line)
    if report.dry_run:
        console.print("re-run with --apply to revoke")


# ── read-only media growth report (Phase 2 / Task 5) ─────────────────────────
#
# Measure first, decide later.  This report opens no media content, parses no document,
# runs no OCR, recomputes no hash and deletes nothing.  It reads stored event metadata and
# the filesystem's own size information, which is why it can run on live state without
# touching a single byte of private content.

#: Age buckets, upper bound in days (``None`` = open ended).
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
