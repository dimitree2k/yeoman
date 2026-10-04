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

import time
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from loguru import logger

from yeoman_gateway.core.pipeline import NextFn, PipelineContext

if TYPE_CHECKING:
    from yeoman_gateway.core.models import InboundEvent
    from yeoman_gateway.knowledge.api import KnowledgeService
    from yeoman_gateway.knowledge.models import TrustedIdentityObservation, TrustedReadContext

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


def build_mention_read_context(
    event: "InboundEvent", *, chat_registry: Any, knowledge: "KnowledgeService"
) -> "TrustedReadContext | None":
    """Build a current read context from bridge identity and the live chat registry."""
    if event.channel != "whatsapp":
        return None
    raw = event.raw_metadata
    account_id = str(raw.get("account_id") or "").strip()
    if not account_id:
        return None

    from yeoman_gateway.knowledge._memory.read_gate import registry_members
    from yeoman_gateway.knowledge.models import TrustedReadContext
    from yeoman_gateway.policy.identity import canonical_user_id

    metadata = {str(key): value for key, value in raw.items()}
    principal = canonical_user_id(event.channel, event.sender_id, metadata)
    members = registry_members(
        chat_registry, channel=event.channel, chat_id=str(event.chat_id)
    )
    if not principal or principal not in members:
        return None
    revision = getattr(knowledge, "policy_revision", None)
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        return None
    try:
        record = chat_registry.get_chat(event.channel, str(event.chat_id))
    except Exception:
        return None
    membership_revision = None
    if isinstance(record, dict):
        membership_revision = str(
            record.get("last_sync_at")
            or record.get("last_seen_at")
            or f"{event.channel}:{event.chat_id}:{len(members)}"
        )
    if not membership_revision:
        return None
    return TrustedReadContext(
        principal_id=principal,
        channel=event.channel,
        chat_id=str(event.chat_id),
        recipient_principals=frozenset(members),
        membership_revision=membership_revision,
        policy_revision=revision,
        purpose="reply",
        now_ms=time.time_ns() // 1_000_000,
        is_direct=not event.is_group,
        owner=False,
    )


class ContactsMiddleware:
    """Resolve the sender identity through the public knowledge facade."""

    def __init__(
        self,
        *,
        knowledge: "KnowledgeService | None" = None,
        observation_issuer: "Callable[[TrustedIdentityObservation], object] | None" = None,
        mention_context_factory: "Callable[[InboundEvent], TrustedReadContext | None] | None" = None,
    ) -> None:
        self._knowledge = knowledge
        # The source authority only verifies an observation that was issued to it.  The
        # composition root hands its issuer in, because this middleware is where the
        # channel's proven metadata becomes an observation.
        self._observation_issuer = observation_issuer
        self._mention_context_factory = mention_context_factory

    async def __call__(self, ctx: PipelineContext, next: NextFn) -> None:
        event = ctx.event

        if event.channel not in _IDENTITY_CHANNELS or self._knowledge is None:
            await next(ctx)
            return

        raw = event.raw_metadata
        new_meta = dict(raw)
        try:
            context = (
                self._mention_context_factory(event)
                if self._mention_context_factory is not None
                else None
            )
            identifiers = self._mentioned_identifiers(event.channel, raw)
            if context is not None and identifiers:
                resolutions = self._knowledge.resolve_mentions(
                    identifiers,
                    at_ms=None,
                    context=context,
                    account_namespace=str(raw.get("account_id") or "").strip(),
                )
                candidates = self._candidate_metadata(
                    resolutions, source="native_identifier"
                )
            else:
                candidates = []

            raw_name_tokens = raw.get("mentioned_name_tokens")
            if (
                context is not None
                and not raw_name_tokens
                and isinstance(event.content, str)
            ):
                candidates.extend(
                    self._candidate_metadata(
                        self._knowledge.search_mention_text_candidates(
                            event.content, context=context
                        ),
                        source="plaintext_alias",
                    )
                )
            if context is not None and isinstance(raw_name_tokens, (list, tuple)):
                for token in raw_name_tokens:
                    if not isinstance(token, str) or not token.strip():
                        continue
                    candidates.extend(
                        self._candidate_metadata(
                            self._knowledge.search_mention_name_candidates(
                                token, context=context
                            ),
                            source="explicit_name_token",
                        )
                    )
            if candidates:
                new_meta["mentioned_person_candidates"] = candidates
        except Exception as exc:
            logger.warning(
                "mention_identity_resolution_failed error_type={}", type(exc).__name__
            )

        if new_meta != raw:
            event = replace(event, raw_metadata=new_meta)
            ctx.event = event

        observation = self._observation(event.channel, event.participant, event.sender_id, raw)
        if observation is None:
            await next(ctx)
            return

        try:
            if self._observation_issuer is not None:
                self._observation_issuer(observation)
            resolution = self._knowledge.resolve_observation(observation)
        except Exception as exc:
            # Knowledge is degraded.  The observation is already durable in the
            # processing journal, so nothing is lost and nothing is invented: this turn
            # simply runs without a proven person.  The log names the reason code only:
            # the error message may carry identifiers.
            logger.warning(
                "identity_resolution_failed channel={} reason={} error_type={}",
                event.channel,
                getattr(exc, "code", None) or "unexpected_error",
                type(exc).__name__,
            )
            await next(ctx)
            return

        if resolution.person_id is None:
            await next(ctx)
            return

        new_meta["contact_id"] = resolution.person_id
        new_meta["identity_status"] = resolution.status
        new_meta["identity_reason"] = resolution.reason
        ctx.event = replace(event, raw_metadata=new_meta)

        await next(ctx)

    @staticmethod
    def _mentioned_identifiers(channel: str, raw: dict[str, Any]):
        from yeoman_gateway.knowledge.models import Identifier

        account_id = str(raw.get("account_id") or "").strip()
        raw_mentions = raw.get("mentioned_jids")
        if (
            channel != "whatsapp"
            or not account_id
            or not isinstance(raw_mentions, (list, tuple))
        ):
            return ()
        identifiers: list[Identifier] = []
        for raw_value in raw_mentions:
            if not isinstance(raw_value, str):
                continue
            value = raw_value.strip()
            kind = (
                "lid"
                if value.endswith("@lid")
                else "phone_jid"
                if _is_phone_jid(value)
                else ""
            )
            if not kind:
                continue
            try:
                identifier = Identifier(
                    channel=channel, kind=kind, value=value, namespace=account_id
                )
            except Exception:
                continue
            if identifier not in identifiers:
                identifiers.append(identifier)
        return tuple(identifiers)

    @staticmethod
    def _candidate_metadata(resolutions: tuple[Any, ...], *, source: str) -> list[dict[str, Any]]:
        return [
            {
                "person_id": result.person_id,
                "status": result.status,
                "reason": result.reason,
                "identity_revision": result.identity_revision,
                "source": source,
            }
            for result in resolutions
            if result.person_id is not None
            and result.status in {"resolved", "ambiguous"}
        ]

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
