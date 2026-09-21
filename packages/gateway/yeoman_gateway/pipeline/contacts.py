"""Contacts identity resolution middleware.

Corresponds to orchestrator stage 3.5: after archive, before reply context.

The middleware resolves the *transport* sender to exactly one person through the public
knowledge facade.  It is a bridge, not an authority:

* it builds a :class:`TrustedIdentityObservation` from what the channel adapter proved -
  the canonical event id, the account namespace, the phone JID, the LID and the push
  name - and hands it to ``knowledge.resolve_observation``;
* it never writes a contact row, never guesses a kind from a bare number, and never
  turns a display name into a mapping;
* when knowledge is unavailable, the observation itself is already durable in the
  processing journal.  The middleware then attaches no person, does not retry, and lets
  the existing degradation path take over.  Losing identity enrichment is acceptable;
  inventing a person is not.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from yeoman_gateway.core.pipeline import NextFn, PipelineContext

if TYPE_CHECKING:
    from yeoman_gateway.knowledge.api import KnowledgeService

# Channels that should trigger contact resolution.
_IDENTITY_CHANNELS = frozenset({"whatsapp", "telegram"})

#: Telegram identifiers are account-scoped in the same way; the namespace names the bot
#: account, which is why it is read from the event rather than derived from the channel.
_DEFAULT_NAMESPACES = {"whatsapp": "whatsapp", "telegram": "telegram"}


def _identifier_kind(channel: str, identifier: str) -> str:
    if channel == "whatsapp":
        return "lid" if identifier.endswith("@lid") else "phone_jid"
    return f"{channel}_id"


def _is_phone_jid(value: str) -> bool:
    return value.endswith("@s.whatsapp.net") or value.endswith("@c.us")


def _push_name(channel: str, raw: dict[str, Any]) -> str | None:
    if channel == "whatsapp":
        return str(raw.get("sender_name") or "").strip() or None
    if channel == "telegram":
        first = str(raw.get("first_name") or "").strip()
        last = str(raw.get("last_name") or "").strip()
        return f"{first} {last}".strip() or None
    return None


class ContactsMiddleware:
    """Resolve the sender identity through the public knowledge facade."""

    def __init__(self, *, knowledge: "KnowledgeService | None" = None) -> None:
        self._knowledge = knowledge

    async def __call__(self, ctx: PipelineContext, next: NextFn) -> None:
        event = ctx.event

        if event.channel not in _IDENTITY_CHANNELS or self._knowledge is None:
            await next(ctx)
            return

        raw = event.raw_metadata
        observation = self._observation(event.channel, event.participant, event.sender_id, raw)
        if observation is None:
            await next(ctx)
            return

        try:
            resolution = self._knowledge.resolve_observation(observation)
        except Exception:
            # Knowledge is degraded.  The observation is already durable in the
            # processing journal, so nothing is lost and nothing is invented: this turn
            # simply runs without a proven person.
            await next(ctx)
            return

        if resolution.person_id is None:
            await next(ctx)
            return

        new_meta = dict(raw)
        new_meta["contact_id"] = resolution.person_id
        new_meta["identity_status"] = resolution.status
        new_meta["identity_reason"] = resolution.reason
        ctx.event = replace(event, raw_metadata=new_meta)

        await next(ctx)

    def _observation(
        self, channel: str, participant: str | None, sender_id: str, raw: dict[str, Any]
    ):
        """Build the typed observation, or ``None`` when there is nothing proven."""
        from yeoman_gateway.knowledge.models import Identifier, TrustedIdentityObservation

        namespace = str(raw.get("account_id") or "").strip() or _DEFAULT_NAMESPACES.get(
            channel, channel
        )
        event_id = str(raw.get("message_id") or "").strip()
        evidence_ref = f"observation:{channel}:{event_id}" if event_id else ""

        phone_raw = str(raw.get("sender_phone_jid") or "").strip()
        lid_raw = str(raw.get("participant_lid") or "").strip()
        candidates: list[tuple[str, str]] = []
        # A phone JID is only a phone JID when the adapter said so.
        if phone_raw and _is_phone_jid(phone_raw):
            candidates.append(("phone_jid", phone_raw))
        if lid_raw.endswith("@lid"):
            candidates.append(("lid", lid_raw))
        primary = str(participant or sender_id or "").strip()
        if primary:
            kind = _identifier_kind(channel, primary)
            if kind != "phone_jid" or _is_phone_jid(primary):
                candidates.append((kind, primary))

        identifiers: list[Identifier] = []
        seen: set[tuple[str, str, str]] = set()
        for kind, value in candidates:
            try:
                identifier = Identifier(channel=channel, kind=kind, value=value, namespace=namespace)
            except Exception:
                # An unparseable identifier is dropped, never reinterpreted.
                continue
            if identifier.full_key in seen:
                continue
            seen.add(identifier.full_key)
            identifiers.append(identifier)
        if not identifiers:
            return None

        # A mapping is only "verified" when the adapter proved it: a phone JID and a LID
        # that arrived together, with no provider-reported conflict.  Everything else is
        # a set of independent observations.
        mapping_verified = (
            len(identifiers) > 1
            and not bool(raw.get("lid_conflict", False))
            and any(item.kind == "phone_jid" for item in identifiers)
            and any(item.kind == "lid" for item in identifiers)
        )
        observed_at = event_timestamp_ms(raw)
        try:
            return TrustedIdentityObservation(
                identifiers=tuple(identifiers),
                evidence_ref=evidence_ref or "observation:unattributed",
                observed_name=_push_name(channel, raw),
                observed_at_ms=observed_at,
                mapping_verified=mapping_verified,
                account_namespace=namespace,
            )
        except Exception:
            return None


def event_timestamp_ms(raw: dict[str, Any]) -> int:
    """The event's own time in milliseconds, or ``0`` when it is not available."""
    value = raw.get("timestamp")
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        number = int(value)
        # Bridge timestamps are milliseconds; a seconds value is scaled, not guessed.
        return number * 1000 if 0 < number < 10_000_000_000 else max(0, number)
    from datetime import datetime

    if isinstance(value, datetime):
        return int(value.timestamp() * 1000)
    return 0
