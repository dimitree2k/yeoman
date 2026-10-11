"""Chat registry CLI commands."""

from __future__ import annotations

import json

import typer
from rich.table import Table

from .core import app, console

chats_app = typer.Typer(help="Manage chat registry")
app.add_typer(chats_app, name="chats")
_CHAT_TYPES = {"group", "dm", "broadcast", "channel"}


@chats_app.command("list")
def chats_list(
    channel: str = typer.Option(None, "--channel", "-c", help="Filter by channel (e.g., whatsapp, telegram)"),
    chat_type: str = typer.Option(None, "--type", "-t", help="Filter by chat type (group, dm, broadcast, channel)"),
    limit: int = typer.Option(50, "--limit", "-l", help="Maximum number of results"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """List chats from captured conversation history."""

    if chat_type and chat_type.lower() not in _CHAT_TYPES:
        console.print(f"[red]Invalid chat type: {chat_type}[/red]")
        console.print(f"[dim]Valid types: {sorted(_CHAT_TYPES)}[/dim]")
        raise typer.Exit(1)
    chats = [
        row for row in _selected_chats()
        if (not channel or channel.lower() == row["channel"])
        and (not chat_type or chat_type.lower() == row["chat_type"])
    ][:max(0, limit)]

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
    chat = next(
        (row for row in _selected_chats() if row["channel"] == channel.lower() and row["chat_id"] == chat_id),
        None,
    )

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


def _selected_chats() -> list[dict]:
    from yeoman_gateway.history.export import cli_secondary_request
    result = cli_secondary_request("history_chats", {})
    return [
        dict(
            row,
            readable_name=row["subject"],
            chat_type="group" if row["chat_id"].endswith("@g.us") else "dm",
            participant_count=None,
            first_seen_at=None,
            last_seen_at=None,
            owner_id=None,
            is_community=False,
            invite_code=None,
        )
        for row in result["chats"]
    ]
