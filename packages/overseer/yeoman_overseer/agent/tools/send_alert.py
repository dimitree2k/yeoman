"""send_alert tool — send via CascadingComms (async), audit-logged."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from yeoman_overseer.alerts.formatting import format_overseer_alert

if TYPE_CHECKING:
    from yeoman_overseer.agent.tools import ToolContext


async def execute(args: dict[str, Any], ctx: ToolContext) -> str:
    """CascadingComms.send is async — this tool must be async too."""
    message = format_overseer_alert(args["message"])
    try:
        await ctx.comms.send(message)
        from yeoman_overseer.audit.logger import AuditEntry
        ctx.audit.append(AuditEntry(
            runbook=getattr(ctx, 'runbook_name', ''),
            trigger="llm",
            action="send_alert",
            target=message[:200],
            result="sent",
            duration_ms=0,
            escalated_to_llm=True,
            domain=getattr(ctx, 'domain', ''),
        ))
        return "[send_alert] sent"
    except Exception as exc:
        return f"[send_alert] ERROR: {exc}"
