"""Contacts identity resolution middleware.

Corresponds to orchestrator stage 3.5: after archive, before reply context.
Ensures every sender has a contact record and attaches contact_id to metadata.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from yeoman.core.pipeline import NextFn, PipelineContext

if TYPE_CHECKING:
    from yeoman.contacts.service import ContactsService

# Channels that should trigger contact resolution.
_IDENTITY_CHANNELS = frozenset({"whatsapp", "telegram"})


def _infer_whatsapp_kind(identifier: str) -> str:
    if identifier.endswith("@lid"):
        return "lid"
    return "phone_jid"


class ContactsMiddleware:
    """Resolve sender identity and ensure contact record exists."""

    def __init__(self, *, contacts: "ContactsService") -> None:
        self._contacts = contacts

    async def __call__(self, ctx: PipelineContext, next: NextFn) -> None:
        event = ctx.event

        if event.channel not in _IDENTITY_CHANNELS:
            await next(ctx)
            return

        # Determine the best identifier for this sender.
        identifier = event.participant or event.sender_id
        if not identifier:
            await next(ctx)
            return

        # Determine kind.
        if event.channel == "whatsapp":
            kind = _infer_whatsapp_kind(identifier)
        else:
            kind = f"{event.channel}_id"

        # Extract push name from raw metadata.
        push_name: str | None = None
        raw = event.raw_metadata
        if event.channel == "whatsapp":
            push_name = str(raw.get("sender_name") or "").strip() or None
        elif event.channel == "telegram":
            first = str(raw.get("first_name") or "").strip()
            last = str(raw.get("last_name") or "").strip()
            push_name = f"{first} {last}".strip() or None

        contact_id = self._contacts.ensure_contact(
            channel=event.channel,
            identifier=identifier,
            kind=kind,
            push_name=push_name,
        )

        # Attach contact_id to metadata for downstream middleware.
        new_meta = dict(event.raw_metadata)
        new_meta["contact_id"] = contact_id
        ctx.event = replace(event, raw_metadata=new_meta)

        await next(ctx)
