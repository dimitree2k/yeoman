"""send_alert tool — send via CascadingComms (async), audit-logged."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from yeoman_overseer.agent.tools import ToolContext


async def execute(args: dict[str, Any], ctx: ToolContext) -> str:
    """CascadingComms.send is async — this tool must be async too."""
    message = args["message"]
    try:
        await ctx.comms.send(message)
        return "[send_alert] sent"
    except Exception as exc:
        return f"[send_alert] ERROR: {exc}"
