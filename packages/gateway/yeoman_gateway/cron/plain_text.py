"""Plain-text cron jobs: send stored text as-is, never through an agent turn.

Group members can schedule these (group reminders), so they must not reach the agent
path, where a scheduled turn runs with every tool and no chat policy.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from yeoman_gateway.bus.events import OutboundMessage
from yeoman_gateway.cron.types import CronJob

JobRunner = Callable[[CronJob], Awaitable[str | None]]

#: Shown in front of a delivered reminder, so the chat sees it is not a new reply.
REMINDER_PREFIX = "⏰ "


class PlainTextDeliveryError(RuntimeError):
    """A plain-text job was not delivered; the message never contains the job text."""


def plain_text_first(fallback: JobRunner, deliver: JobRunner) -> JobRunner:
    """Run plain-text jobs through ``deliver`` and every other job through ``fallback``."""

    async def run(job: CronJob) -> str | None:
        if job.payload.kind == "plain_text":
            return await deliver(job)
        return await fallback(job)

    return run


def plain_text_deliverer(*, service_effects: Any | None, bus: Any) -> JobRunner:
    """Send a plain-text job's message to its own chat and fail unless it was sent."""

    async def deliver(job: CronJob) -> str | None:
        channel = str(job.payload.channel or "").strip()
        chat_id = str(job.payload.to or "").strip()
        if not job.payload.deliver or not channel or not chat_id:
            raise PlainTextDeliveryError("plain_text job needs an explicit target")
        if job.payload.source_chat and job.payload.source_chat != chat_id:
            raise PlainTextDeliveryError("plain_text job target differs from its source chat")
        content = REMINDER_PREFIX + job.payload.message
        if service_effects is None:
            await bus.publish_outbound(
                OutboundMessage(channel=channel, chat_id=chat_id, content=content)
            )
            return None
        receipt = await service_effects.send(
            source="cron",
            operation_ref=f"cron:{job.id}:{job.state.next_run_at_ms or ''}",
            channel=channel,
            chat_id=chat_id,
            content=content,
        )
        # None means the chat is on the legacy queue path, which gives no receipt.
        state = getattr(receipt, "state", "sent") if receipt is not None else "sent"
        if state != "sent":
            raise PlainTextDeliveryError(f"group reminder not delivered: {state}")
        return None

    return deliver


def failure_alert_label(job: CronJob) -> str:
    """How an alert names a job; plain-text jobs never reveal their text or name."""
    if job.payload.kind == "plain_text":
        return f"Group reminder {job.id}"
    return f"Cron job '{job.name}'"


def failure_alert_error(job: CronJob) -> str | None:
    """The error to show in an alert, or None when it must stay out of the owner DM."""
    if job.payload.kind == "plain_text":
        return None
    return str(job.state.last_error or "unknown error")
