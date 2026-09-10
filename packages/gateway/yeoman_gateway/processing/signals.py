"""Canonical mapping of WhatsApp provider signals into journal events (Plan 04, R01).

The mapper is pure: it turns one bridge payload into a :class:`JournalSignal` and nothing
else. It performs no policy check, opens no turn and touches no effect - the journal is the
only place it writes, and only through ``ProcessingStore.append_event``.

Determinism rules:

* the event id is the digest of the provider identity, so a replay after a restart produces
  the same id instead of a second event,
* a reference is only set when the signal really carries it; a missing reference stays
  missing and becomes an unresolved relation rather than a guessed parent,
* recipient tokens are stored hashed, never as raw JIDs.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from yeoman_gateway.processing.models import (
    DELIVERED_STATUSES_TUPLE as _DELIVERED,
)
from yeoman_gateway.processing.models import (
    TransportReceipt,
    canonical_hash,
)

CHANNEL = "whatsapp"

#: Signal kinds the bridge can report (the canonical event kinds of spec R01).
SIGNAL_KINDS: tuple[str, ...] = ("message", "edit", "reaction", "delete", "receipt")


@dataclass(frozen=True, slots=True)
class ReceiptEvidence:
    """A delivery/read/played fact about one provider message.

    It is *additional* evidence: it never proves that the transport accepted the message
    and therefore never moves an effect to ``sent``.
    """

    kind: str  # delivered | read | played
    provider_message_id: str
    recipient_token: str
    occurred_ms: int | None = None

    @property
    def detail(self) -> str:
        return f"recipient={self.recipient_token}"


@dataclass(frozen=True, slots=True)
class JournalSignal:
    """One canonical provider signal, before it reaches the journal."""

    kind: str
    event_key: str
    event_id: str
    trace_id: str
    channel: str
    chat_id: str
    principal: str
    occurred_ms: int | None = None
    source_message_id: str | None = None
    target_message_id: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    def to_event_payload(self) -> dict[str, Any]:
        """Mapping the journal stores; references travel so relations can resolve later."""
        body: dict[str, Any] = {
            "kind": self.kind,
            "origin": "whatsapp_bridge",
            "principal": self.principal,
            "channel": self.channel,
            "chat_id": self.chat_id,
            "occurred_ms": self.occurred_ms,
            "target_message_id": self.target_message_id,
        }
        body.update(dict(self.payload))
        return body


def signal_event_id(event_key: str) -> str:
    """Stable id for one provider identity."""
    return hashlib.sha256(event_key.encode("utf-8")).hexdigest()[:32]


def _first(payload: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _token(value: str | None) -> str:
    """Local part of a JID, so the journal key stays stable across alias forms."""
    if not value:
        return ""
    return value.split("@", 1)[0].strip()


def _hashed_token(value: str | None) -> str:
    """Recipient tokens are stored hashed: the journal never needs the raw JID."""
    token = _token(value)
    if not token:
        return "unknown"
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def _to_ms(value: Any) -> int | None:
    """Bridge timestamps are epoch seconds; tolerate milliseconds and strings."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return int(number * 1000) if number < 1e11 else int(number)


class WhatsAppSignalMapper:
    """Maps bridge payloads of one kind into canonical journal signals."""

    def __init__(self, *, channel: str = CHANNEL) -> None:
        self._channel = channel

    def map(self, payload: Mapping[str, Any], *, kind: str) -> JournalSignal | None:
        if kind not in SIGNAL_KINDS:
            raise ValueError(f"unknown signal kind: {kind}")
        if not isinstance(payload, Mapping):
            raise TypeError("signal payload must be a mapping")

        chat_id = _first(payload, "chatJid", "chat_jid", "chat", "remoteJid")
        if not chat_id:
            return None

        if kind == "message":
            return self._message(payload, chat_id)
        if kind == "edit":
            return self._edit(payload, chat_id)
        if kind == "delete":
            return self._delete(payload, chat_id)
        if kind == "reaction":
            return self._reaction(payload, chat_id)
        return self._receipt(payload, chat_id)

    # -- kinds -------------------------------------------------------------------------

    def _message(self, payload: Mapping[str, Any], chat_id: str) -> JournalSignal | None:
        message_id = _first(payload, "messageId", "message_id", "id")
        if not message_id:
            return None
        principal = _token(_first(payload, "senderId", "participantJid", "sender", "from"))
        event_key = f"{self._channel}:{chat_id}:message:{message_id}"
        body = {
            "text": _first(payload, "text", "content") or "",
            "is_group": bool(payload.get("isGroup")) or chat_id.endswith("@g.us"),
            "mentioned_bot": bool(payload.get("mentionedBot")),
            "reply_to_message_id": _first(payload, "replyToMessageId", "reply_to_message_id"),
        }
        return self._signal(
            kind="message",
            event_key=event_key,
            chat_id=chat_id,
            principal=principal,
            payload=payload,
            body=body,
            source_message_id=message_id,
            target_message_id=body["reply_to_message_id"],
        )

    def _edit(self, payload: Mapping[str, Any], chat_id: str) -> JournalSignal | None:
        message_id = _first(payload, "messageId", "message_id", "id")
        if not message_id:
            return None
        edit_ms = _to_ms(payload.get("timestamp") or payload.get("editTimestamp"))
        principal = _token(_first(payload, "senderId", "participantJid", "sender", "from"))
        event_key = f"{self._channel}:{chat_id}:edit:{message_id}:{edit_ms or 0}"
        return self._signal(
            kind="edit",
            event_key=event_key,
            chat_id=chat_id,
            principal=principal,
            payload=payload,
            body={"text": _first(payload, "text", "content") or ""},
            source_message_id=message_id,
            target_message_id=message_id,
            occurred_ms=edit_ms,
        )

    def _delete(self, payload: Mapping[str, Any], chat_id: str) -> JournalSignal | None:
        message_id = _first(payload, "messageId", "message_id", "id", "targetMessageId")
        if not message_id:
            return None
        principal = _token(_first(payload, "senderId", "participantJid", "sender", "from"))
        event_key = f"{self._channel}:{chat_id}:delete:{message_id}"
        return self._signal(
            kind="delete",
            event_key=event_key,
            chat_id=chat_id,
            principal=principal,
            payload=payload,
            body={"revoked": True},
            source_message_id=message_id,
            target_message_id=message_id,
        )

    def _reaction(self, payload: Mapping[str, Any], chat_id: str) -> JournalSignal | None:
        target = _first(payload, "targetMessageId", "target_message_id")
        sender = _first(payload, "senderId", "participantJid", "sender", "from")
        emoji = _first(payload, "emoji", "text", "reaction") or ""
        timestamp = _to_ms(payload.get("timestamp"))
        if not target and not (sender and emoji):
            return None
        event_key = (
            f"{self._channel}:{chat_id}:reaction:{target or 'unknown'}:"
            f"{_token(sender) or 'unknown'}:{emoji}:{timestamp or 0}"
        )
        return self._signal(
            kind="reaction",
            event_key=event_key,
            chat_id=chat_id,
            principal=_token(sender),
            payload=payload,
            body={"emoji": emoji, "removed": not bool(emoji)},
            source_message_id=_first(payload, "messageId", "message_id", "id"),
            target_message_id=target,
            occurred_ms=timestamp,
        )

    def _receipt(self, payload: Mapping[str, Any], chat_id: str) -> JournalSignal | None:
        message_id = _first(payload, "messageId", "message_id", "id")
        if not message_id:
            return None
        recipient = _first(payload, "recipientJid", "recipient", "participantJid", "to")
        status = (_first(payload, "status", "receiptType") or "delivered").lower()
        event_key = (
            f"{self._channel}:{chat_id}:receipt:{message_id}:"
            f"{_hashed_token(recipient)}:{status}"
        )
        return self._signal(
            kind="receipt",
            event_key=event_key,
            chat_id=chat_id,
            principal=_token(recipient),  # the receiving participant, never the bot
            payload=payload,
            body={"status": status, "recipient_token": _hashed_token(recipient)},
            source_message_id=message_id,
            target_message_id=message_id,
        )

    # -- receipt evidence --------------------------------------------------------------

    def receipt_evidence(
        self, receipt: TransportReceipt | None, signals: Iterable[Any]
    ) -> tuple[ReceiptEvidence, ...]:
        """Delivery/read/played facts for one provider message.

        A receipt that arrived *before* the transport receipt is not lost: it is already in
        the journal and is picked up here by provider id once the mapping exists.
        """
        if receipt is None or not receipt.provider_message_id:
            return ()
        found: list[ReceiptEvidence] = []
        for signal in signals:
            payload = dict(getattr(signal, "payload", None) or {})
            status = str(payload.get("status") or "").lower()
            if status not in _DELIVERED:
                continue
            if str(payload.get("recipient_token") or "") == "" and not payload.get("recipient_token"):
                token = "unknown"
            else:
                token = str(payload.get("recipient_token"))
            found.append(
                ReceiptEvidence(
                    kind="delivered" if status == "delivered" else status,
                    provider_message_id=receipt.provider_message_id,
                    recipient_token=token,
                    occurred_ms=getattr(signal, "occurred_ms", None),
                )
            )
        # Deduplicate so the same recipient and status does not grow the evidence forever.
        seen: set[tuple[str, str]] = set()
        unique: list[ReceiptEvidence] = []
        for item in found:
            key = (item.kind, item.recipient_token)
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
        return tuple(unique)

    # -- shared ------------------------------------------------------------------------

    def _signal(
        self,
        *,
        kind: str,
        event_key: str,
        chat_id: str,
        principal: str,
        payload: Mapping[str, Any],
        body: Mapping[str, Any],
        source_message_id: str | None = None,
        target_message_id: str | None = None,
        occurred_ms: int | None = None,
    ) -> JournalSignal:
        occurred = occurred_ms if occurred_ms is not None else _to_ms(payload.get("timestamp"))
        return JournalSignal(
            kind=kind,
            event_key=event_key,
            event_id=signal_event_id(event_key),
            trace_id=f"{self._channel}:{chat_id}:{canonical_hash(event_key)[:12]}",
            channel=self._channel,
            chat_id=chat_id,
            principal=principal,
            occurred_ms=occurred,
            source_message_id=source_message_id,
            target_message_id=target_message_id,
            payload=dict(body),
        )


__all__ = [
    "CHANNEL",
    "SIGNAL_KINDS",
    "JournalSignal",
    "ReceiptEvidence",
    "SignalJournalSink",
    "attach_receipt_evidence",
    "WhatsAppSignalMapper",
    "signal_event_id",
]


class SignalJournalSink:
    """Journals provider signals. It runs no policy, opens no turn and creates no effect."""

    def __init__(
        self,
        store: Any,
        *,
        mapper: WhatsAppSignalMapper | None = None,
        clock: Any = None,
    ) -> None:
        self._store = store
        self._mapper = mapper or WhatsAppSignalMapper()
        self._clock = clock

    def __call__(self, kind: str, payload: Mapping[str, Any]) -> str | None:
        if self._store is None:
            return None
        signal = self._mapper.map(payload, kind=kind)
        if signal is None:
            return None
        now = int(self._clock()) if self._clock is not None else None
        return self._store.append_event(
            event_key=signal.event_key,
            event_id=signal.event_id,
            trace_id=signal.trace_id,
            payload=signal.to_event_payload(),
            now_ms=now,
        )


def attach_receipt_evidence(
    store: Any,
    effect_id: str,
    *,
    now_ms: int,
    mapper: WhatsAppSignalMapper | None = None,
) -> tuple[ReceiptEvidence, ...]:
    """Attach late delivery/read evidence to an effect; the state never changes here.

    A receipt that arrived before the transport receipt was correlated is picked up now by
    provider message id, so no evidence is lost and none is invented. ``record_evidence``
    is the only writer, and it never moves an effect to ``sent`` - that stays with the
    transport or a probe.
    """
    receipt = store.effect_transport_receipt(effect_id)
    if receipt is None or not receipt.provider_message_id:
        return ()
    signals = store.delivery_signals(
        chat_id=receipt.chat_id, message_id=receipt.provider_message_id
    )
    evidence = (mapper or WhatsAppSignalMapper()).receipt_evidence(receipt, signals)
    attached: list[ReceiptEvidence] = []
    for item in evidence:
        existing = {
            str(entry.detail or "")
            for entry in (store.list_effects(states=None, limit=500) or ())
            if entry.effect_id == effect_id
            for entry in entry.evidence
        }
        if f"{item.kind}:{item.detail}" in existing:
            continue
        store.record_evidence(
            effect_id,
            kind=item.kind,
            now_ms=now_ms,
            detail=f"{item.kind}:{item.detail}",
        )
        attached.append(item)
    return tuple(attached)
