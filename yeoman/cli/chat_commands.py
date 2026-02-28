"""Chat registry CLI commands."""

from __future__ import annotations

import json

import typer
from rich.table import Table

from .core import app, console

chats_app = typer.Typer(help="Manage chat registry")
app.add_typer(chats_app, name="chats")


@chats_app.command("list")
def chats_list(
    channel: str = typer.Option(None, "--channel", "-c", help="Filter by channel (e.g., whatsapp, telegram)"),
    chat_type: str = typer.Option(None, "--type", "-t", help="Filter by chat type (group, dm, broadcast, channel)"),
    limit: int = typer.Option(50, "--limit", "-l", help="Maximum number of results"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """List chats from the registry."""
    from yeoman.storage.chat_registry import ChatRegistry, ChatType

    registry = ChatRegistry()

    filters: dict[str, str] = {}
    if channel:
        filters["channel"] = channel.lower()
    if chat_type:
        try:
            filters["chat_type"] = ChatType(chat_type.lower()).value
        except ValueError:
            console.print(f"[red]Invalid chat type: {chat_type}[/red]")
            console.print(f"[dim]Valid types: {[t.value for t in ChatType]}[/dim]")
            raise typer.Exit(1)

    chats = registry.list_chats(limit=limit, **filters)

    if json_output:
        console.print(json.dumps(chats, indent=2, default=str))
    else:
        table = Table(title=f"Chat Registry ({len(chats)} chats)")
        table.add_column("Channel", style="cyan")
        table.add_column("Type", style="magenta")
        table.add_column("ID", style="green")
        table.add_column("Name", style="yellow")
        table.add_column("Participants", style="blue")
        table.add_column("First Seen", style="dim")

        for chat in chats:
            table.add_row(
                chat["channel"],
                chat["chat_type"],
                chat["chat_id"][:30] + "..." if len(chat["chat_id"]) > 30 else chat["chat_id"],
                chat["readable_name"] or "N/A",
                str(chat["participant_count"]) if chat["participant_count"] else "N/A",
                chat["first_seen_at"][:19] if chat["first_seen_at"] else "N/A",
            )

        console.print(table)


@chats_app.command("show")
def chats_show(
    chat_id: str = typer.Argument(..., help="Chat ID to show"),
    channel: str = typer.Option("whatsapp", "--channel", "-c", help="Channel (default: whatsapp)"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """Show detailed information about a chat."""
    from yeoman.storage.chat_registry import ChatRegistry

    registry = ChatRegistry()
    chat = registry.get_chat(channel.lower(), chat_id)

    if not chat:
        console.print(f"[red]Chat not found: {chat_id}[/red]")
        raise typer.Exit(1)

    if json_output:
        console.print(json.dumps(chat, indent=2, default=str))
    else:
        console.print("[bold]Chat Details[/bold]")
        console.print(f"  Channel: {chat['channel']}")
        console.print(f"  Type: {chat['chat_type']}")
        console.print(f"  ID: {chat['chat_id']}")
        console.print(f"  Name: {chat['readable_name'] or 'N/A'}")
        console.print(f"  First Seen: {chat['first_seen_at']}")
        console.print(f"  Last Seen: {chat['last_seen_at'] or 'N/A'}")
        console.print(f"  Description: {chat['description'] or 'N/A'}")
        console.print(f"  Owner: {chat['owner_id'] or 'N/A'}")
        console.print(f"  Participants: {chat['participant_count'] or 'N/A'}")
        console.print(f"  Community: {'Yes' if chat['is_community'] else 'No'}")
        console.print(f"  Invite Code: {chat['invite_code'] or 'N/A'}")

        if chat.get("metadata_json"):
            try:
                metadata = json.loads(chat["metadata_json"])
                console.print("\\n[bold]Bridge Metadata:[/bold]")
                console.print(json.dumps(metadata, indent=2, default=str))
            except json.JSONDecodeError:
                console.print(f"\\n[dim]Raw metadata: {chat['metadata_json']}[/dim]")


@chats_app.command("sync")
def chats_sync(
    channel: str = typer.Option("whatsapp", "--channel", "-c", help="Channel to sync (default: whatsapp)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without applying changes"),
) -> None:
    """Sync chat metadata from bridge to registry."""
    import asyncio
    import time
    import uuid

    import websockets

    from yeoman.config.loader import load_config
    from yeoman.storage.chat_registry import ChatRegistry

    console.print(f"[cyan]Syncing metadata from {channel} bridge...[/cyan]")

    config = load_config()
    registry = ChatRegistry()

    channel_config = getattr(config.channels, channel, None)
    if not channel_config or not getattr(channel_config, "enabled", False):
        console.print(f"[red]{channel.title()} channel not enabled in config[/red]")
        raise typer.Exit(1)

    bridge_url = str(channel_config.resolved_bridge_url).strip()
    bridge_token = str(getattr(channel_config, "bridge_token", "") or "").strip()

    if not bridge_url or not bridge_token:
        console.print(f"[red]Bridge URL or token not configured for {channel}[/red]")
        raise typer.Exit(1)

    async def _fetch_groups(url: str, token: str) -> list[dict]:
        request_id = uuid.uuid4().hex
        payload = {
            "version": 2,
            "type": "list_groups",
            "token": token,
            "requestId": request_id,
            "accountId": "default",
            "payload": {"ids": []},
        }
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps(payload))
            deadline = time.monotonic() + 10.0
            while True:
                timeout = deadline - time.monotonic()
                if timeout <= 0:
                    raise TimeoutError("Bridge did not reply in time")
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                data = json.loads(raw)
                if data.get("version") != 2:
                    continue
                if data.get("type") != "response":
                    continue
                if data.get("requestId") != request_id:
                    continue
                response_payload = data.get("payload")
                if not isinstance(response_payload, dict):
                    raise RuntimeError("Bridge response payload malformed")
                if not bool(response_payload.get("ok")):
                    error = response_payload.get("error")
                    raise RuntimeError(f"Bridge returned error: {error}")
                result = response_payload.get("result")
                if not isinstance(result, dict):
                    raise RuntimeError("Bridge response result malformed")
                groups = result.get("groups", [])
                if not isinstance(groups, list):
                    raise RuntimeError("Groups should be a list")
                return groups

    try:
        groups = asyncio.run(_fetch_groups(bridge_url, bridge_token))
    except Exception as e:
        console.print(f"[red]Failed to fetch groups from bridge: {e}[/red]")
        raise typer.Exit(1)

    if dry_run:
        console.print(f"[yellow]Dry run: Would sync {len(groups)} chats[/yellow]")
        for group in groups[:5]:
            console.print(f"  - {group.get('chatJid')}: {group.get('subject')}")
        if len(groups) > 5:
            console.print(f"  ... and {len(groups) - 5} more")
    else:
        results = registry.sync_from_bridge_metadata(channel.lower(), groups)
        new_count = sum(1 for value in results.values() if value)
        updated_count = len(results) - new_count
        console.print(f"[green]✓[/green] Synced {len(results)} chats from {channel} bridge")
        console.print(f"  [green]{new_count}[/green] new chats registered")
        console.print(f"  [cyan]{updated_count}[/cyan] existing chats updated")
