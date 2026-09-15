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

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from loguru import logger

from yeoman_gateway.processing.participation import ParticipationOpportunity

#: Keys a stored message row may use for its identity.
_ID_KEYS: tuple[str, ...] = ("message_id", "event_id", "id")


@dataclass(frozen=True, slots=True)
class ParticipationContextBounds:
    """Validated bounds for one context build."""

    window_minutes: int = 120
    max_messages: int = 40
    max_anchors: int = 10


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
    ) -> None:
        self._archive = archive
        self._policy = policy
        self._anchors = anchors
        self._taste = taste
        self._clock = clock

    async def build(
        self,
        opportunity: ParticipationOpportunity,
        *,
        bounds: ParticipationContextBounds | None = None,
        allowed_actions: Sequence[str] | None = None,
        now_ms: int | None = None,
    ) -> dict[str, object]:
        """The bounded context for one opportunity.

        ``messages`` holds same-chat retained sources including intervening
        participants, ``anchors`` holds delivered Arvid text only, and the remaining
        keys are the trusted decision inputs (allowed actions, remaining budgets,
        guidance, revisions). Truncation is reported instead of hidden.
        """
        limits = bounds or ParticipationContextBounds()
        moment = int(now_ms if now_ms is not None else _now_ms())
        since = datetime.fromtimestamp(
            (moment - int(limits.window_minutes) * 60_000) / 1000, UTC
        )
        until = datetime.fromtimestamp(moment / 1000, UTC)
        rows = self._archive.lookup_messages_in_range(
            opportunity.channel,
            opportunity.chat_id,
            since,
            until,
            limit=max(1, min(int(limits.max_messages) * 4, 300)),
            latest=True,
        )
        ordered = sorted(
            (row for row in rows if isinstance(row, Mapping)),
            key=lambda row: (float(row.get("timestamp") or 0), str(row.get("message_id") or "")),
        )
        by_id = {_row_id(row): row for row in ordered if _row_id(row)}

        # Required sources first: the batch that triggered this decision is never
        # dropped in favour of older optional context.
        required: list[dict[str, object]] = []
        optional: list[dict[str, object]] = []
        for source_id in opportunity.source_event_ids:
            row = by_id.get(str(source_id))
            if row is not None:
                required.append(row)
        required_ids = {_row_id(row) for row in required}
        for row in ordered:
            if _row_id(row) in required_ids:
                continue
            optional.append(row)
        selected = required + optional
        truncated = 0
        if len(selected) > int(limits.max_messages):
            keep = selected[: int(limits.max_messages)]
            keep_ids = {_row_id(row) for row in keep}
            # A required source always survives truncation.
            missing_required = [
                row for row in required if _row_id(row) not in keep_ids
            ]
            keep = (missing_required + keep)[: int(limits.max_messages)]
            truncated = len(selected) - len(keep)
            selected = keep

        messages = [_render_message(row) for row in selected if _row_id(row)]
        anchors: list[dict[str, object]] = []
        if self._anchors is not None:
            raw_anchors = await self._anchors.delivered_anchors(
                opportunity.channel,
                opportunity.chat_id,
                since_ms=int(since.timestamp() * 1000),
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

        resolved_actions = tuple(allowed_actions or ("silence",))
        context: dict[str, object] = {
            "channel": opportunity.channel,
            "chat_id": opportunity.chat_id,
            "opportunity_id": opportunity.opportunity_id,
            "trigger": opportunity.trigger,
            "lane": opportunity.lane,
            "messages": messages,
            "anchors": anchors,
            "allowed_actions": list(resolved_actions),
            "allowed_contribution_types": list(self._allowed_contribution_types(opportunity)),
            "budgets": dict(self._budgets(opportunity)),
            "guidance": self._guidance(opportunity),
            "policy_revision": self._policy_revision(opportunity),
            "context_revision": int(opportunity.observed_revision),
            "truncated_messages": truncated,
            "dropped_source_ids": [
                str(item)
                for item in opportunity.source_event_ids
                if str(item) not in {_row_id(row) for row in selected}
            ][:32],
        }
        taste = await self._advisory_taste(opportunity)
        if taste:
            context["advisory_taste"] = taste
        return context

    # -- trusted inputs ----------------------------------------------------------------

    def _resolved(self, opportunity: ParticipationOpportunity) -> Any:
        try:
            return self._policy.resolve_participation(opportunity.channel, opportunity.chat_id)
        except Exception:
            logger.warning("participation context: policy resolution failed")
            return None

    def _guidance(self, opportunity: ParticipationOpportunity) -> str:
        resolved = self._resolved(opportunity)
        return str(getattr(resolved, "guidance", "") or "")

    def _allowed_contribution_types(
        self, opportunity: ParticipationOpportunity
    ) -> tuple[str, ...]:
        """Existing configured spontaneity action vocabulary for this chat."""
        try:
            resolved = self._policy.resolve_policy(opportunity.channel, opportunity.chat_id)
        except Exception:
            return ()
        actions = getattr(resolved, "spontaneity_allowed_actions", None)
        if actions is None:
            # Fall back to the same default vocabulary the legacy planner uses for
            # this profile, so the judge sees the effective action set.
            from yeoman_gateway.consciousness.tools import ConsciousnessTools

            profile = str(getattr(resolved, "spontaneity_profile", "") or "").strip()
            return tuple(sorted(ConsciousnessTools._default_allowed_actions(profile)))  # noqa: SLF001
        return tuple(str(item) for item in actions)

    def _budgets(self, opportunity: ParticipationOpportunity) -> dict[str, int]:
        resolved = self._resolved(opportunity)
        if resolved is None:
            return {}
        return {
            "comments_per_window": int(
                getattr(resolved, "max_unsolicited_comments_per_window", 0)
            ),
            "window_minutes": int(getattr(resolved, "comment_window_minutes", 0)),
            "reactions_per_window": int(getattr(resolved, "max_reactions_per_window", 0)),
            "judge_calls_per_hour": int(
                getattr(resolved, "max_unaddressed_judge_calls_per_hour", 0)
            ),
        }

    def _policy_revision(self, opportunity: ParticipationOpportunity) -> str:
        engine = self._policy
        for attribute in ("policy_version", "version", "policy_hash"):
            value = getattr(engine, attribute, None)
            if value:
                return str(value)
        return ""

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


__all__ = ["ParticipationContextBounds", "ParticipationContextBuilder"]
