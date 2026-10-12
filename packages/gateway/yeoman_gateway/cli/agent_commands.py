"""Direct agent interaction command."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import typer

from yeoman_gateway import __logo__
from yeoman_gateway.history.control import MAX_IPC_REQUEST_BYTES

from .core import app, console


async def _request_agent_turn(socket_path: Path, prompt: str, session_key: str) -> str:
    request = json.dumps({
        "cmd": "trigger_agent_turn",
        "args": {
            "prompt": prompt,
            "session_key": session_key,
            "channel": "cli",
            "chat_id": "direct",
        },
    }).encode() + b"\n"
    if len(request) > MAX_IPC_REQUEST_BYTES:
        raise typer.BadParameter("Agent request is too large")

    writer = None
    try:
        reader, writer = await asyncio.open_unix_connection(
            str(socket_path), limit=MAX_IPC_REQUEST_BYTES
        )
        writer.write(request)
        await writer.drain()
        line = await reader.readline()
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(
            "Gateway agent unavailable. If interrupted, the turn may have completed; "
            "check the session before retrying."
        ) from exc
    except asyncio.LimitOverrunError as exc:
        raise typer.BadParameter("Gateway agent returned an oversized response") from exc
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    try:
        envelope: Any = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise typer.BadParameter("Gateway agent returned an invalid response") from None
    if not isinstance(envelope, dict):
        raise typer.BadParameter("Gateway agent returned an invalid response")
    if envelope.get("status") != "ok":
        detail = envelope.get("code", envelope.get("message", envelope.get("status")))
        raise typer.BadParameter(f"Gateway agent refused the request: {detail}")
    result = envelope.get("response")
    if not isinstance(result, dict) or not isinstance(result.get("response"), str):
        raise typer.BadParameter("Gateway agent returned an invalid response")
    return result["response"]


@app.command()
def agent(
    message: str = typer.Option(None, "--message", "-m", help="Message to send to the agent"),
    session_id: str = typer.Option("cli:default", "--session", "-s", help="Session ID"),
) -> None:
    """Interact with the agent directly."""
    from yeoman_shared.config.loader import load_config

    socket_path = Path(load_config().ipc.gateway_socket_path).expanduser()
    if message:
        response = asyncio.run(_request_agent_turn(socket_path, message, session_id))
        console.print(f"\n{__logo__} {response}")
        return

    console.print(f"{__logo__} Interactive mode (Ctrl+C to exit)\n")

    async def run_interactive() -> None:
        while True:
            try:
                user_input = console.input("[bold blue]You:[/bold blue] ")
                if not user_input.strip():
                    continue
                response = await _request_agent_turn(socket_path, user_input, session_id)
                console.print(f"\n{__logo__} {response}\n")
            except KeyboardInterrupt:
                console.print("\nGoodbye!")
                break

    asyncio.run(run_interactive())
