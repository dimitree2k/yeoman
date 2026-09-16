"""Confirmed social anchors from the participation ledger.

A "delivered anchor" is a bot text that a participant provably received in one
exact chat: it exists only for a reservation in state ``delivered``, which the
ledger only reaches from authenticated recipient evidence. Transport acceptance,
a provider message id, a handled boolean and a historical ``sent`` row are not
delivery and never produce an anchor (spec section 9).
"""

from __future__ import annotations

from typing import Any

from yeoman_gateway.consciousness.log import SpeakupLog
from yeoman_gateway.processing.store import ProcessingStore

#: Receipt statuses that prove a recipient received the message.
_DELIVERED_RECEIPT_STATUSES: frozenset[str] = frozenset({"delivered", "read", "played"})

#: Receipt statuses that prove the transport rejected it before acceptance.
_REJECTED_RECEIPT_STATUSES: frozenset[str] = frozenset({"failed", "error", "rejected"})


def is_group_chat(channel: str, chat_id: str) -> bool:
    """Whether delivery evidence for this target can only prove "at least one"."""
    value = str(chat_id)
    if str(channel) == "whatsapp":
        return value.endswith("@g.us")
    if str(channel) == "telegram":
        return value.startswith("-")
    return False


class DeliveryAnchorReader:
    """Joins ledger delivery truth with the processing store's effect payloads."""

    def __init__(self, *, log: SpeakupLog, store: ProcessingStore) -> None:
        self._log = log
        self._store = store

    async def delivered_anchors(
        self,
        channel: str,
        chat_id: str,
        *,
        since_ms: int,
        limit: int = 20,
    ) -> list[dict[str, object]]:
        """Confirmed same-chat bot text anchors, newest first.

        Each result carries ``effect_id``, ``provider_message_id``,
        ``delivered_at_ms``, ``message``, ``channel``, ``chat_id``,
        ``evidence_kind``, ``evidence_ref`` and ``delivery_state``. It claims no
        thread or task authority: callers must resolve that separately.
        """
        rows = await self._log.delivered_reservation_rows(
            channel=channel,
            chat_id=chat_id,
            since_ms=int(since_ms),
            limit=max(1, int(limit)),
        )
        if not rows:
            return []
        effects = {
            str(item["effect_id"]): item
            for item in self._store.delivered_text_effects(
                channel=channel,
                chat_id=chat_id,
                since_ms=int(since_ms),
                limit=max(1, int(limit)) * 4,
            )
        }
        anchors: list[dict[str, object]] = []
        for row in rows:
            effect_id = str(row["effect_id"])
            effect = effects.get(effect_id)
            if effect is None or str(effect.get("state")) != "sent":
                # Ledger says delivered but the effect store cannot confirm a text
                # effect for this exact chat: report nothing rather than guess.
                continue
            text = effect.get("text")
            if text is None:
                continue
            provider_message_id = str(row["provider_message_id"] or "").strip()
            social_closed = False
            for anchor_id in (provider_message_id, effect_id):
                if not anchor_id:
                    continue
                try:
                    social_closed = await self._log.social_anchor_closed(
                        channel=str(row["channel"]),
                        chat_id=str(row["chat_id"]),
                        anchor_message_id=anchor_id,
                    )
                except Exception:  # noqa: BLE001 - unknown closure fails closed
                    social_closed = True
                if social_closed:
                    break
            anchors.append(
                {
                    "effect_id": effect_id,
                    "provider_message_id": row["provider_message_id"],
                    "delivered_at_ms": row["delivered_at_ms"],
                    "message": str(text),
                    "channel": str(row["channel"]),
                    "chat_id": str(row["chat_id"]),
                    "evidence_kind": row["evidence_kind"],
                    "evidence_ref": row["evidence_ref"],
                    "evidence_scope": (
                        "at_least_one"
                        if str(row["evidence_kind"]) == "group_delivery_at_least_one"
                        else "exact_target"
                    ),
                    "delivery_state": "delivered",
                    "social_closed": social_closed,
                    "source_ids": (),
                }
            )
        return _deduplicate(anchors, limit=max(1, int(limit)))


def _deduplicate(anchors: list[dict[str, Any]], *, limit: int) -> list[dict[str, object]]:
    """One anchor per effect, keeping the newest evidence, bounded by ``limit``."""
    seen: set[str] = set()
    ordered = sorted(
        anchors,
        key=lambda item: (-int(item["delivered_at_ms"] or 0), str(item["effect_id"])),
    )
    result: list[dict[str, object]] = []
    for anchor in ordered:
        key = str(anchor["effect_id"])
        if key in seen:
            continue
        seen.add(key)
        result.append(anchor)
        if len(result) >= limit:
            break
    return result


class ParticipationReceiptReconciler:
    """Projects processing-store receipt truth into the participation ledger.

    The processing store owns transport truth; the ledger owns participation
    accounting. Each receipt or delivery signal is projected through its stable
    effect id, so a repeated callback, a replayed signal or an interrupted
    projection never charges the allowance twice or publishes a second anchor.
    Nothing here invents evidence: no provider lookup, no timer assumption.
    """

    def __init__(self, *, log: SpeakupLog, store: ProcessingStore) -> None:
        self._log = log
        self._store = store

    async def reconcile(self, *, limit: int = 50, now_ms: int | None = None) -> dict[str, int]:
        """One bounded reconciliation pass. Returns per-outcome counters."""
        import time as _time

        moment = int(now_ms if now_ms is not None else _time.time() * 1000)
        counters = {"accepted": 0, "delivered": 0, "released": 0, "retained": 0, "skipped": 0}
        for row in await self._log.pending_delivery_reservations(limit=limit):
            effect_id = str(row["effect_id"])
            proposal_id = str(row["proposal_id"])
            state = str(row["delivery_state"])
            if state in {"transport_accepted", "delivery_unknown"}:
                if await self._project_recipient_evidence(
                    proposal_id=proposal_id,
                    effect_id=effect_id,
                    channel=str(row["channel"]),
                    chat_id=str(row["chat_id"]),
                    now_ms=moment,
                ):
                    counters["delivered"] += 1
                else:
                    counters["retained"] += 1
                continue
            effect = self._store.get_effect(effect_id)
            if effect is None:
                # No durable effect exists for this reservation: the crash-recovery
                # case "reservation saved, effect absent". It is never sent from
                # here; the submitting path revalidates and reuses the same id.
                counters["skipped"] += 1
                continue
            if effect.state == "sent":
                transport = self._store.effect_transport_receipt(effect_id)
                await self._log.project_transport_accepted(
                    proposal_id,
                    effect_id=effect_id,
                    provider_message_id=(
                        None if transport is None else transport.provider_message_id
                    ),
                    evidence_kind="transport_receipt",
                    evidence_ref=(
                        effect_id if transport is None else (transport.receipt_id or effect_id)
                    ),
                    now_ms=moment,
                )
                counters["accepted"] += 1
                if await self._project_recipient_evidence(
                    proposal_id=proposal_id,
                    effect_id=effect_id,
                    channel=str(row["channel"]),
                    chat_id=str(row["chat_id"]),
                    now_ms=moment,
                ):
                    counters["delivered"] += 1
                continue
            if effect.state in {"failed", "cancelled", "expired"} and state == "submitted":
                if await self._log.release_delivery(
                    proposal_id,
                    effect_id=effect_id,
                    state="failed",
                    reason=f"effect_{effect.state}",
                    now_ms=moment,
                ):
                    counters["released"] += 1
                continue
            if effect.state == "unknown":
                await self._log.note_delivery_unknown(
                    proposal_id,
                    effect_id=effect_id,
                    evidence_kind="dispatch_unknown",
                    evidence_ref=effect_id,
                    now_ms=moment,
                )
                counters["retained"] += 1
                continue
            counters["skipped"] += 1
        return counters

    async def _project_recipient_evidence(
        self,
        *,
        proposal_id: str,
        effect_id: str,
        channel: str,
        chat_id: str,
        now_ms: int,
    ) -> bool:
        transport = self._store.effect_transport_receipt(effect_id)
        provider_id = None if transport is None else transport.provider_message_id
        if not provider_id:
            return False
        group = is_group_chat(channel, chat_id)
        for signal in self._store.delivery_signals(
            chat_id=chat_id, message_id=provider_id, limit=20
        ):
            payload = dict(getattr(signal, "payload", None) or {})
            status = str(payload.get("status") or "").lower()
            kind = str(getattr(signal, "kind", "") or "")
            if kind == "reaction":
                evidence_kind = "reaction_proof"
            elif status in _DELIVERED_RECEIPT_STATUSES:
                evidence_kind = (
                    "group_delivery_at_least_one"
                    if group
                    else ("recipient_read" if status == "read" else "recipient_delivery")
                )
            else:
                continue
            occurred = getattr(signal, "occurred_ms", None)
            return await self._log.project_recipient_delivery(
                proposal_id,
                effect_id=effect_id,
                provider_message_id=provider_id,
                evidence_kind=evidence_kind,
                evidence_ref=str(getattr(signal, "event_id", "") or effect_id),
                now_ms=int(occurred) if occurred else now_ms,
            )
        return False
