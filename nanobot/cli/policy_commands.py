"""Policy CLI commands."""

from __future__ import annotations

import json
from datetime import datetime

import typer

from .channel_commands import _ensure_whatsapp_bridge_token
from .core import app, console, make_policy_engine

policy_app = typer.Typer(help="Manage chat policy")
app.add_typer(policy_app, name="policy")


def _policy_known_tools() -> set[str]:
    """Known top-level tools for policy diagnostics."""
    return {
        "read_file",
        "write_file",
        "edit_file",
        "list_dir",
        "exec",
        "pi_stats",
        "web_search",
        "web_fetch",
        "deep_research",
        "message",
        "spawn",
        "cron",
    }


@policy_app.command("path")
def policy_path_cmd() -> None:
    """Show policy file location."""
    from nanobot.policy.loader import get_policy_path

    console.print(get_policy_path())


@policy_app.command("explain")
def policy_explain(
    channel: str = typer.Option(..., "--channel", "-c", help="Channel: telegram or whatsapp"),
    chat_id: str = typer.Option(..., "--chat", help="Chat ID"),
    sender_id: str = typer.Option(..., "--sender", "-s", help="Sender ID"),
    is_group: bool = typer.Option(False, "--group", help="Treat as group chat"),
    mentioned_bot: bool = typer.Option(False, "--mentioned", help="Message mentions the bot"),
    reply_to_bot: bool = typer.Option(False, "--reply-to-bot", help="Message is a reply to bot"),
) -> None:
    """Explain merged policy + decision for one actor/chat."""
    from nanobot.adapters.policy_engine import EnginePolicyAdapter
    from nanobot.config.loader import load_config

    config = load_config()
    policy_engine, policy_path = make_policy_engine(config)
    policy_adapter = EnginePolicyAdapter(
        engine=policy_engine,
        known_tools=_policy_known_tools(),
        policy_path=policy_path,
        reload_on_change=False,
    )
    report = policy_adapter.explain(
        channel=channel,
        chat_id=chat_id,
        sender_id=sender_id,
        is_group=is_group,
        mentioned_bot=mentioned_bot,
        reply_to_bot=reply_to_bot,
    )
    console.print_json(json.dumps(report, ensure_ascii=False, indent=2))


@policy_app.command("cmd")
def policy_cmd(
    command: str = typer.Argument(..., help='Canonical slash command, e.g. "/policy list-groups"'),
    dry_run: bool = typer.Option(False, "--dry-run", help="Run command in dry-run mode"),
    confirm: bool = typer.Option(False, "--confirm", help="Confirm risky command execution"),
) -> None:
    """Execute one shared policy admin command via CLI."""
    import getpass

    from nanobot.config.loader import load_config
    from nanobot.policy.admin.contracts import PolicyActorContext, PolicyExecutionOptions
    from nanobot.policy.admin.service import PolicyAdminService

    config = load_config()
    policy_engine, policy_path = make_policy_engine(config)
    if policy_path is None:
        console.print("[red]Policy path unavailable[/red]")
        raise typer.Exit(1)

    apply_channels = (
        policy_engine.apply_channels if policy_engine is not None else {"telegram", "whatsapp"}
    )
    service = PolicyAdminService(
        policy_path=policy_path,
        workspace=config.workspace_path,
        known_tools=_policy_known_tools(),
        apply_channels=apply_channels,
        on_policy_applied=None,
    )
    result = service.execute_from_text(
        command,
        actor=PolicyActorContext(
            source="cli",
            channel="cli",
            chat_id="local",
            sender_id=getpass.getuser(),
            is_group=False,
            is_owner=True,
        ),
        options=PolicyExecutionOptions(dry_run=dry_run, confirm=confirm),
    )
    if result.message:
        console.print(result.message)
    if result.outcome in {"invalid", "error", "denied"}:
        raise typer.Exit(1)


@policy_app.command("annotate-whatsapp-comments")
def policy_annotate_whatsapp_comments(
    overwrite: bool = typer.Option(False, "--overwrite", help="Overwrite existing comment fields"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show changes without writing policy.json"
    ),
    bridge_url: str | None = typer.Option(
        None, "--bridge-url", help="WhatsApp bridge ws:// URL (default: from config)"
    ),
) -> None:
    """Fill WhatsApp group chat comments in policy.json using the running bridge."""
    import asyncio
    import shutil
    import time
    import uuid

    import websockets

    from nanobot.config.loader import load_config
    from nanobot.policy.loader import get_policy_path, load_policy, save_policy

    async def _list_groups(url: str, ids: list[str], token: str) -> dict[str, str]:
        request_id = uuid.uuid4().hex
        payload = {
            "version": 2,
            "type": "list_groups",
            "token": token,
            "requestId": request_id,
            "accountId": "default",
            "payload": {"ids": ids},
        }
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps(payload))
            deadline = time.monotonic() + 10.0
            while True:
                timeout = deadline - time.monotonic()
                if timeout <= 0:
                    raise TimeoutError("bridge did not reply in time")
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
                    raise RuntimeError("bridge response payload malformed")
                if not bool(response_payload.get("ok")):
                    error = response_payload.get("error")
                    raise RuntimeError(f"bridge returned error: {error}")
                result = response_payload.get("result")
                if not isinstance(result, dict):
                    raise RuntimeError("bridge response result malformed")
                groups = result.get("groups", [])
                out: dict[str, str] = {}
                if isinstance(groups, list):
                    for item in groups:
                        if not isinstance(item, dict):
                            continue
                        gid = str(item.get("chatJid", "")).strip()
                        subj = str(item.get("subject", "")).strip()
                        if gid and subj:
                            out[gid] = subj
                return out

    config = load_config()
    resolved_bridge_url = bridge_url or config.channels.whatsapp.resolved_bridge_url
    bridge_token = _ensure_whatsapp_bridge_token(config=config)

    policy_path = get_policy_path()
    policy = load_policy(policy_path)

    wa = policy.channels.get("whatsapp")
    if not wa or not wa.chats:
        console.print("[yellow]No WhatsApp chats found in policy.json[/yellow]")
        return

    chat_ids = [cid for cid in wa.chats.keys() if isinstance(cid, str) and cid.endswith("@g.us")]
    if not chat_ids:
        console.print("[yellow]No WhatsApp group chat IDs (*@g.us) found in policy.json[/yellow]")
        return

    targets: list[str] = []
    for chat_id in chat_ids:
        ov = wa.chats.get(chat_id)
        current = getattr(ov, "comment", None) if ov else None
        if overwrite or not (isinstance(current, str) and current.strip()):
            targets.append(chat_id)

    if not targets:
        console.print("[green]✓[/green] Nothing to do (all groups already have comments).")
        return

    try:
        names = asyncio.run(_list_groups(resolved_bridge_url, targets, bridge_token))
    except Exception as e:
        console.print(f"[red]Failed to fetch group names from bridge:[/red] {e}")
        console.print(
            "[dim]Tip: ensure the WhatsApp bridge is running and connected (nanobot channels bridge status).[/dim]"
        )
        raise typer.Exit(1)

    updated = 0
    missing: list[str] = []
    for chat_id in targets:
        name = names.get(chat_id)
        if not name:
            missing.append(chat_id)
            continue
        wa.chats[chat_id].comment = name
        updated += 1

    if dry_run:
        console.print(f"[yellow]Dry run.[/yellow] Would update {updated} group(s).")
        if missing:
            console.print(f"[dim]No name returned for {len(missing)} group(s).[/dim]")
        return

    if policy_path.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = policy_path.with_name(f"{policy_path.name}.bak-{stamp}")
        shutil.copy2(policy_path, backup_path)
        console.print(f"[green]✓[/green] Backup written: {backup_path}")

    save_policy(policy, policy_path)
    console.print(f"[green]✓[/green] Updated policy comments for {updated} group(s): {policy_path}")
    if missing:
        console.print(f"[yellow]Warning:[/yellow] no name returned for {len(missing)} group(s).")
