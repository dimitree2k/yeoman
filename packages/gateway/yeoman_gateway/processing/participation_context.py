"""Source-backed context for one participation decision.

The judge and the unsolicited generator both read from here. Two invariants matter
more than the shape of the result:

* **Same chat only.** Every query is an exact ``(channel, chat_id)`` query. A record
  from another channel that happens to share a chat id is never returned, and a
  record from another chat in the same channel is never returned.
* **No unverified statements.** Only a *delivered* Arvid message becomes an anchor.
  A draft, a preview, a submitted or transport-accepted-only effect and a historical
  ``sent`` row without recipient evidence are not things Arvid said.

The builder assembles a bounded, JSON-compatible dict and reports what it truncated.
It performs no network research, no long-term personal/contact memory recall and no
taste distillation call; the only advisory input it may include is same-chat taste
that already carries reliable provenance, and the retrieval itself stays optional.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from yeoman_gateway.policy.engine import ParticipationSnapshot
from yeoman_gateway.processing.participation import (
    ParticipationDecisionError,
    ParticipationOpportunity,
)

#: Keys a stored message row may use for its identity.
_ID_KEYS: tuple[str, ...] = ("message_id", "event_id", "id")


@dataclass(frozen=True, slots=True)
class ParticipationContextBounds:
    """Validated bounds for one context build."""

    window_minutes: int = 120
    max_messages: int = 40
    max_anchors: int = 10


@dataclass(frozen=True, slots=True)
class ParticipationDecisionInputs:
    """One trusted, immutable input bundle shared by preflight and context build."""

    snapshot: ParticipationSnapshot
    bounds: ParticipationContextBounds
    allowed_actions: tuple[str, ...]
    allowed_intents: frozenset[str]
    remaining_budgets: tuple[tuple[str, int], ...]
    reservation_limits_by_intent: tuple[
        tuple[str, tuple[tuple[str, int, int] | tuple[str, int, int, str], ...]], ...
    ]
    approval_required: bool
    arbitration_revision: int
    current_source_ids: tuple[str, ...]
    continuation_candidate: bool

    def __post_init__(self) -> None:
        """Normalize collection fields at the trust boundary."""
        object.__setattr__(
            self,
            "allowed_actions",
            tuple(str(item) for item in (self.allowed_actions or ())),
        )
        object.__setattr__(
            self,
            "allowed_intents",
            frozenset(str(item) for item in (self.allowed_intents or ())),
        )
        object.__setattr__(
            self,
            "remaining_budgets",
            _freeze_budget_pairs(self.remaining_budgets),
        )
        object.__setattr__(
            self,
            "reservation_limits_by_intent",
            _validate_reservation_limits(self.reservation_limits_by_intent),
        )
        object.__setattr__(
            self,
            "current_source_ids",
            tuple(str(item) for item in self.current_source_ids if str(item).strip()),
        )


class ParticipationContextBuilder:
    """Builds the bounded judge/generator view from archived sources and anchors."""

    def __init__(
        self,
        *,
        archive: Any,
        policy: Any,
        anchors: Any | None = None,
        taste: Any | None = None,
        clock: Any | None = None,
        source_authorizer: Callable[[Mapping[str, Any]], bool] | None = None,
    ) -> None:
        self._archive = archive
        # Kept on the constructor for old object construction; decision inputs are the
        # sole source of trusted policy values during ``build``.
        self._policy = policy
        self._anchors = anchors
        self._taste = taste
        self._clock = clock
        self._source_authorizer = source_authorizer

    async def build(
        self,
        opportunity: ParticipationOpportunity,
        *,
        inputs: ParticipationDecisionInputs,
        now_ms: int | None = None,
    ) -> dict[str, object]:
        """The bounded context for one opportunity.

        ``messages`` holds same-chat retained sources including intervening
        participants, ``anchors`` holds delivered Arvid text only, and the remaining
        keys are the trusted decision inputs (allowed actions, remaining budgets,
        guidance, revisions). Truncation is reported instead of hidden.
        """
        limits = inputs.bounds
        moment = int(now_ms if now_ms is not None else _now_ms())
        since = datetime.fromtimestamp(
            (moment - int(limits.window_minutes) * 60_000) / 1000, UTC
        )
        until = datetime.fromtimestamp(moment / 1000, UTC)
        since_ms = int(since.timestamp()) * 1000
        rows = self._archive.lookup_messages_in_range(
            opportunity.channel,
            opportunity.chat_id,
            since,
            until,
            limit=max(1, min(int(limits.max_messages) * 4, 300)),
            latest=True,
        )
        range_rows = [row for row in rows if isinstance(row, Mapping)]

        # Resolve every required source by its exact archive key. The bounded history
        # query is only an optional-context optimization and cannot hide a trigger.
        required_ids = _unique_ids(opportunity.source_event_ids)
        required_rows: dict[str, dict[str, object]] = {}
        for source_id in required_ids:
            row = self._archive.lookup_message(
                opportunity.channel, opportunity.chat_id, source_id
            )
            if not isinstance(row, Mapping) or _row_id(row) != source_id:
                raise ParticipationDecisionError("source_unavailable", detail=source_id)
            rejection = self._source_rejection(
                row,
                source_id=source_id,
                opportunity=opportunity,
                since_ms=since_ms,
                until_ms=moment,
                snapshot=inputs.snapshot,
            )
            if rejection:
                raise ParticipationDecisionError(rejection, detail=source_id)
            required_rows[source_id] = dict(row)

        optional: list[dict[str, object]] = []
        seen_optional: set[str] = set(required_rows)
        for row in range_rows:
            source_id = _row_id(row)
            if not source_id or source_id in seen_optional:
                continue
            if str(row.get("channel") or "") != str(opportunity.channel):
                continue
            if str(row.get("chat_id") or "") != str(opportunity.chat_id):
                continue
            if self._source_rejection(
                row,
                source_id=source_id,
                opportunity=opportunity,
                since_ms=since_ms,
                until_ms=moment,
                snapshot=inputs.snapshot,
            ):
                # Optional sources are data, not authority: remove them before prompt
                # construction, while required-source failures remain hard errors.
                continue
            optional.append(dict(row))
            seen_optional.add(source_id)

        ordered = sorted(
            [*required_rows.values(), *optional], key=_row_sort_key
        )
        required = [row for row in ordered if _row_id(row) in set(required_ids)]
        optional = [row for row in ordered if _row_id(row) not in set(required_ids)]
        max_messages = max(0, int(limits.max_messages))
        kept_required = required[-max_messages:] if max_messages else []
        free = max(0, max_messages - len(kept_required))
        selected = kept_required + (optional[-free:] if free else [])
        selected.sort(key=_row_sort_key)
        selected_ids = {_row_id(row) for row in selected}
        dropped_ids = [
            _row_id(row)
            for row in ordered
            if _row_id(row) and _row_id(row) not in selected_ids
        ]
        truncated = len(ordered) - len(selected)

        messages = [_render_message(row) for row in selected if _row_id(row)]
        anchors: list[dict[str, object]] = []
        if self._anchors is not None:
            raw_anchors = await self._anchors.delivered_anchors(
                opportunity.channel,
                opportunity.chat_id,
                since_ms=since_ms,
                limit=int(limits.max_anchors),
            )
            for anchor in raw_anchors:
                if not isinstance(anchor, Mapping):
                    continue
                if str(anchor.get("delivery_state") or "") != "delivered":
                    # Only recipient evidence creates a statement of Arvid's.
                    continue
                if not anchor.get("provider_message_id"):
                    continue
                anchors.append(dict(anchor))

        current_source_ids = _unique_ids(inputs.current_source_ids)
        allowed_intents = tuple(sorted(str(item) for item in inputs.allowed_intents))
        budgets = dict(inputs.remaining_budgets)
        context: dict[str, object] = {
            "channel": opportunity.channel,
            "chat_id": opportunity.chat_id,
            "opportunity_id": opportunity.opportunity_id,
            "trigger": opportunity.trigger,
            "lane": opportunity.lane,
            "messages": messages,
            "anchors": anchors,
            "allowed_actions": list(inputs.allowed_actions),
            "allowed_intents": list(allowed_intents),
            "allowed_contribution_types": list(
                _snapshot_value(inputs.snapshot, "allowed_contribution_types", ())
            ),
            "budgets": budgets,
            "remaining_budgets": budgets,
            "reservation_limits_by_intent": _render_reservation_limits(
                inputs.reservation_limits_by_intent
            ),
            "approval_required": bool(inputs.approval_required),
            "arbitration_revision": int(inputs.arbitration_revision),
            "continuation_candidate": bool(inputs.continuation_candidate),
            "current_source_ids": list(current_source_ids),
            "source_event_ids": list(current_source_ids),
            "guidance": _snapshot_guidance(inputs.snapshot),
            "policy_revision": _snapshot_policy_revision(inputs.snapshot),
            "context_revision": int(opportunity.observed_revision),
            "truncated_messages": truncated,
            "dropped_source_ids": dropped_ids[:32],
            "dropped_source_count": len(dropped_ids),
            "direct_addressed": bool(_snapshot_value(inputs.snapshot, "direct_addressed", False)),
            "allows_continuation": "continue" in inputs.allowed_intents,
        }
        taste = await self._advisory_taste(opportunity)
        if taste:
            context["advisory_taste"] = taste
        return context

    # -- source trust ------------------------------------------------------------------

    def _source_rejection(
        self,
        row: Mapping[str, Any],
        *,
        source_id: str,
        opportunity: ParticipationOpportunity,
        since_ms: int,
        until_ms: int,
        snapshot: Any,
    ) -> str | None:
        if str(row.get("channel") or "") != str(opportunity.channel):
            return "source_not_authorized"
        if str(row.get("chat_id") or "") != str(opportunity.chat_id):
            return "source_not_authorized"
        sender = str(row.get("sender_id") or row.get("participant") or "").strip()
        if not sender:
            return "source_not_authorized"
        timestamp_ms = _row_timestamp_ms(row)
        if timestamp_ms is None:
            return "source_unavailable"
        if timestamp_ms < since_ms or timestamp_ms > until_ms:
            return "source_expired"
        if not self._source_is_authorized(
            row,
            source_id=source_id,
            opportunity=opportunity,
            sender=sender,
            snapshot=snapshot,
        ):
            return "source_not_authorized"
        return None

    def _source_is_authorized(
        self,
        row: Mapping[str, Any],
        *,
        source_id: str,
        opportunity: ParticipationOpportunity,
        sender: str,
        snapshot: Any,
    ) -> bool:
        source_map = _snapshot_value(snapshot, "source_authorized", None)
        if isinstance(source_map, Mapping) and source_id in source_map:
            return bool(source_map[source_id])
        blocked = {
            str(item)
            for item in (_snapshot_value(snapshot, "blocked_senders", ()) or ())
        }
        if sender in blocked:
            return False
        allowed = _snapshot_value(snapshot, "allowed_senders", None)
        if allowed is not None and sender not in {str(item) for item in allowed}:
            return False
        if self._source_authorizer is None:
            # A sender that is not blocked is not proof that this source was admitted.
            # Participation context needs either immutable per-source evidence or a
            # current row-level authorization check; without one, fail closed.
            return False
        try:
            return bool(self._source_authorizer(row))
        except Exception:
            return False

    async def _advisory_taste(
        self, opportunity: ParticipationOpportunity
    ) -> list[dict[str, object]]:
        """Same-chat advisory taste, only when it already carries provenance.

        Retrieval must stay cheap and must never require a classifier or a
        distillation call (spec section 5).
        """
        if self._taste is None:
            return []
        try:
            hits = self._taste(opportunity.channel, opportunity.chat_id)
        except Exception:
            return []
        if not isinstance(hits, Iterable):
            return []
        patterns: list[dict[str, object]] = []
        for hit in hits:
            if not isinstance(hit, Mapping):
                continue
            provenance = str(hit.get("provenance") or "")
            if not provenance:
                # Old unverified patterns are not authoritative new guidance.
                continue
            patterns.append(
                {
                    "content": str(hit.get("content") or ""),
                    "provenance": provenance,
                    "confidence": hit.get("confidence"),
                }
            )
        return patterns[:5]


def _row_id(row: Mapping[str, Any]) -> str:
    for key in _ID_KEYS:
        value = row.get(key)
        if value:
            return str(value)
    return ""


def _unique_ids(values: Iterable[Any]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return tuple(result)


def _row_timestamp_ms(row: Mapping[str, Any]) -> int | None:
    value = row.get("timestamp")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        # InboundArchive stores Unix seconds; test doubles often use milliseconds.
        return int(number * 1000 if abs(number) < 100_000_000_000 else number)
    created_at = str(row.get("created_at") or "").strip()
    if not created_at:
        return None
    try:
        parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def _row_sort_key(row: Mapping[str, Any]) -> tuple[int, str]:
    return (_row_timestamp_ms(row) or 0, _row_id(row))


def _freeze_budget_pairs(value: Any) -> tuple[tuple[str, int], ...]:
    entries = value.items() if isinstance(value, Mapping) else (value or ())
    result: list[tuple[str, int]] = []
    for entry in entries:
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            continue
        result.append((str(entry[0]), int(entry[1])))
    return tuple(result)


def _validate_reservation_limits(
    value: Any,
) -> tuple[
    tuple[str, tuple[tuple[str, int, int] | tuple[str, int, int, str], ...]], ...
]:
    """Validate the immutable tuple shape accepted by the participation ledger."""
    field = "reservation_limits_by_intent"
    if not isinstance(value, tuple):
        raise TypeError(f"{field} must be a tuple")
    for group in value:
        if (
            not isinstance(group, tuple)
            or len(group) != 2
            or not isinstance(group[0], str)
            or not isinstance(group[1], tuple)
        ):
            raise TypeError(f"{field} entries must be (intent, tuple[limits, ...])")
        for limit in group[1]:
            if not isinstance(limit, tuple) or len(limit) not in (3, 4):
                raise TypeError(f"{field} limit entries must have 3 or 4 tuple items")
            if not isinstance(limit[0], str) or any(
                not isinstance(item, int) or isinstance(item, bool) for item in limit[1:3]
            ):
                raise TypeError(f"{field} limit entries need (category, int, int[, kind])")
            if len(limit) == 4 and not isinstance(limit[3], str):
                raise TypeError(f"{field} limit window kind must be a string")
    return value


def _snapshot_value(snapshot: Any, name: str, default: Any = None) -> Any:
    if isinstance(snapshot, Mapping):
        return snapshot.get(name, default)
    return getattr(snapshot, name, default)


def _snapshot_participation(snapshot: Any) -> Any:
    return _snapshot_value(snapshot, "participation", None)


def _snapshot_guidance(snapshot: Any) -> str:
    direct = _snapshot_value(snapshot, "guidance", None)
    value = direct if direct is not None else _snapshot_value(_snapshot_participation(snapshot), "guidance", "")
    return str(value or "")


def _snapshot_policy_revision(snapshot: Any) -> str:
    for name in ("policy_version", "version", "policy_hash"):
        value = _snapshot_value(snapshot, name, "")
        if value:
            return str(value)
    return ""


def _render_reservation_limits(value: Any) -> dict[str, list[list[Any]]]:
    result: dict[str, list[list[Any]]] = {}
    for intent, limits in value:
        result[intent] = [list(limit) for limit in limits]
    return result


def _render_message(row: Mapping[str, Any]) -> dict[str, object]:
    """One context message. Media summaries are reused, never invented."""
    text = str(row.get("text") or "").strip()
    media_summary = ""
    if "[image_description]" in text:
        media_summary = text
        text = ""
    return {
        "event_id": _row_id(row),
        "message_id": _row_id(row),
        "sender": str(row.get("sender_name") or row.get("sender_id") or row.get("participant") or "?"),
        "sender_id": str(row.get("sender_id") or row.get("participant") or ""),
        "text": text,
        "media_summary": media_summary,
        "timestamp": row.get("timestamp"),
        "channel": str(row.get("channel") or ""),
        "chat_id": str(row.get("chat_id") or ""),
    }


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)


__all__ = [
    "ParticipationContextBounds",
    "ParticipationContextBuilder",
    "ParticipationDecisionInputs",
]
