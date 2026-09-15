"""The single runtime entrypoint for one autonomous participation decision.

``ParticipationRuntime`` (in ``consciousness/participation_runtime.py``) is the
observer-facing, non-awaiting offer path. This module is the other half: it takes an
admitted opportunity and performs exactly the work its decision requires - nothing on
silence, a reaction effect for a reaction, one draft and one effect for a comment.

Boundaries enforced here, in order:

1. hard preflight (feature/chat enabled, pause, sender access, feasible actions);
2. the shadow lane stops after judging: no generation, tool, typing, effect, preview
   or taste write may happen in shadow;
3. exactly one judge attempt, charged to the persisted hourly quota;
4. a ``comment`` acquires the ledger reservation *before* generation and submits one
   stable managed effect; a ``react`` submits one reaction effect with a receipt;
5. everything is recorded through the ledger, and the processing store stays the
   owner of transport truth.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from loguru import logger

from yeoman_gateway.consciousness.log import deterministic_effect_id
from yeoman_gateway.processing.participation import (
    ParticipationDecision,
    ParticipationDecisionError,
    ParticipationJudge,
    ParticipationOpportunity,
)

#: Counters that must stay distinguishable in operational traces (spec section 12).
COUNTERS: tuple[str, ...] = (
    "admitted",
    "preflight_skipped",
    "judge_attempted",
    "deliberate_silence",
    "judge_failed",
    "reaction_selected",
    "comment_selected",
    "generated",
    "shadow_decision",
    "duplicate_suppressed",
)


class ParticipationBlockedError(RuntimeError):
    """The opportunity cannot proceed. The reason is a stable code."""

    def __init__(self, reason: str) -> None:
        self.reason = str(reason)
        super().__init__(self.reason)


class SnapshotProvider(Protocol):
    """Resolves the effective participation snapshot for one exact target."""

    def __call__(
        self,
        channel: str,
        chat_id: str,
        *,
        epoch: int,
        opportunity: "ParticipationOpportunity | None" = None,
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ParticipationAdmission:
    """Trusted record of what the model decided about which exact target."""

    opportunity_id: str
    channel: str
    chat_id: str
    activation_epoch: int
    lane: str
    observed_revision: int
    action: str
    intent: str
    purpose: str = ""
    emoji: str | None = None
    target_message_id: str | None = None
    anchor_message_id: str | None = None


class ParticipationRuntime:
    """One decision, then the minimum work it requires. Disabled by default."""

    def __init__(
        self,
        *,
        judge: ParticipationJudge | None,
        context_builder: Any,
        ledger: Any,
        snapshot_provider: SnapshotProvider,
        is_paused: Callable[[str, str], str | None] | None = None,
        is_source_allowed: Callable[[str, str, Sequence[str]], bool] | None = None,
        source_principals: Callable[[str, str, Sequence[str]], Sequence[str]] | None = None,
        is_participant_allowed: Callable[[str, str, str], bool] | None = None,
        submission: Any | None = None,
        reactor: Any | None = None,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self._judge = judge
        self._context_builder = context_builder
        self._ledger = ledger
        self._snapshot_provider = snapshot_provider
        self._is_paused = is_paused or (lambda channel, chat_id: None)
        self._is_source_allowed = is_source_allowed or (
            lambda channel, chat_id, sources: True
        )
        self._source_principals = source_principals or (
            lambda channel, chat_id, sources: ()
        )
        self._is_participant_allowed = is_participant_allowed or (
            lambda channel, chat_id, sender: True
        )
        self._submission = submission
        self._reactor = reactor
        self._clock_ms = clock_ms or _now_ms
        self._counters: dict[str, int] = {}

    # -- observability -----------------------------------------------------------------

    def counters(self) -> dict[str, int]:
        return dict(self._counters)

    def _count(self, name: str) -> None:
        self._counters[name] = self._counters.get(name, 0) + 1

    # -- entrypoint --------------------------------------------------------------------

    async def evaluate_participation(
        self, opportunity: ParticipationOpportunity
    ) -> dict[str, object]:
        """Evaluate one admitted opportunity. Never raises for a decision failure."""
        self._count("admitted")
        try:
            snapshot = dict(
                self._snapshot_provider(
                    opportunity.channel,
                    opportunity.chat_id,
                    epoch=int(opportunity.activation_epoch),
                    opportunity=opportunity,
                )
            )
        except ParticipationBlockedError as blocked:
            self._count("preflight_skipped")
            await self._record(opportunity, "preflight_skipped", blocked.reason)
            return {"status": "skipped", "reason": blocked.reason}
        except Exception:
            self._count("preflight_skipped")
            await self._record(opportunity, "preflight_skipped", "snapshot_error")
            return {"status": "skipped", "reason": "snapshot_error"}

        try:
            self._preflight(opportunity, snapshot)
        except ParticipationBlockedError as blocked:
            self._count("preflight_skipped")
            await self._record(opportunity, "preflight_skipped", blocked.reason)
            return {"status": "skipped", "reason": blocked.reason}

        lane = str(snapshot.get("lane") or opportunity.lane)
        if not self._judge:
            self._count("preflight_skipped")
            await self._record(opportunity, "preflight_skipped", "no_judge")
            return {"status": "skipped", "reason": "no_judge"}

        context = await self._context_builder.build(opportunity)
        attempt_id = f"{opportunity.opportunity_id}:0"
        charged = await self._ledger.reserve_judge_attempt(
            attempt_id,
            opportunity_id=opportunity.opportunity_id,
            channel=opportunity.channel,
            chat_id=opportunity.chat_id,
            now_ms=int(self._clock_ms()),
            hourly_limit=int(snapshot.get("judge_calls_per_hour", 0)),
            min_gap_ms=int(snapshot.get("min_gap_seconds", 0)) * 1000,
            continuation_candidate=bool(snapshot.get("continuation_candidate", False)),
            continuation_reserve=int(snapshot.get("continuation_reserve", 0)),
        )
        if not charged:
            self._count("duplicate_suppressed")
            await self._record(opportunity, "skipped", "attempt_not_available")
            return {"status": "skipped", "reason": "attempt_not_available"}
        self._count("judge_attempted")

        try:
            decision = await self._judge.decide(opportunity, context)
        except ParticipationDecisionError as failure:
            self._count("judge_failed")
            await self._ledger.record_judge_outcome(attempt_id, outcome=failure.reason)
            await self._record(opportunity, "judge_failed", failure.reason)
            return {"status": "judge_failed", "reason": failure.reason}
        except Exception as exc:  # noqa: BLE001 - one chat must not stop the queue
            self._count("judge_failed")
            logger.warning(
                "participation_judge_error chat={} error_type={}",
                opportunity.chat_id,
                type(exc).__name__,
            )
            await self._ledger.record_judge_outcome(attempt_id, outcome="provider_error")
            await self._record(opportunity, "judge_failed", "provider_error")
            return {"status": "judge_failed", "reason": "provider_error"}
        await self._ledger.record_judge_outcome(attempt_id, outcome=decision.action)

        if lane == "shadow":
            # Shadow records the decision and stops: no generation, effect, preview,
            # typing or taste write may be caused by a shadow decision.
            self._count("shadow_decision")
            await self._record(opportunity, f"shadow_{decision.action}", decision.reason)
            return {
                "status": "shadow",
                "action": decision.action,
                "intent": decision.intent,
            }

        if decision.action == "silence":
            self._count("deliberate_silence")
            await self._record(opportunity, "decided_silence", decision.reason)
            return {"status": "silence", "intent": decision.intent}

        if decision.action == "react":
            self._count("reaction_selected")
            return await self._run_reaction(opportunity, snapshot, decision)

        self._count("comment_selected")
        return await self._run_comment(opportunity, snapshot, decision, context)

    # -- boundaries --------------------------------------------------------------------

    def _preflight(
        self, opportunity: ParticipationOpportunity, snapshot: Mapping[str, Any]
    ) -> None:
        if not bool(snapshot.get("enabled", False)):
            raise ParticipationBlockedError("feature_disabled")
        if not bool(snapshot.get("opted_in", False)):
            raise ParticipationBlockedError("chat_not_opted_in")
        if str(snapshot.get("invalid_reason") or ""):
            raise ParticipationBlockedError(str(snapshot["invalid_reason"]))
        if int(snapshot.get("activation_epoch", opportunity.activation_epoch)) != int(
            opportunity.activation_epoch
        ):
            # A different epoch is a different owner generation: stale work is dead.
            raise ParticipationBlockedError("epoch_changed")
        pause = self._is_paused(opportunity.channel, opportunity.chat_id)
        if pause:
            raise ParticipationBlockedError(str(pause))
        if not self._is_source_allowed(
            opportunity.channel, opportunity.chat_id, opportunity.source_event_ids
        ):
            raise ParticipationBlockedError("source_not_authorized")
        # The originating participants must still be authorized *themselves*: a
        # service principal that may transport an effect is not a substitute for the
        # sender's access (spec section 3.1).
        for sender in self._source_principals(
            opportunity.channel, opportunity.chat_id, opportunity.source_event_ids
        ):
            if not self._is_participant_allowed(opportunity.channel, opportunity.chat_id, sender):
                raise ParticipationBlockedError("source_principal_not_authorized")
        actions = snapshot.get("allowed_actions")
        if isinstance(actions, (list, tuple)) and not set(actions) - {"silence"}:
            # Only silence is feasible: do not spend a provider call to be told that.
            raise ParticipationBlockedError("no_feasible_action")

    async def _run_reaction(
        self,
        opportunity: ParticipationOpportunity,
        snapshot: Mapping[str, Any],
        decision: ParticipationDecision,
    ) -> dict[str, object]:
        if self._reactor is None or not decision.target_message_id or not decision.emoji:
            await self._record(opportunity, "reaction_skipped", "no_reaction_path")
            return {"status": "reaction_skipped", "reason": "no_reaction_path"}
        effect_id = deterministic_effect_id(
            channel=opportunity.channel,
            chat_id=opportunity.chat_id,
            operation="reaction",
            proposal_id=opportunity.opportunity_id,
        )
        reserved = await self._ledger.reserve_delivery(
            proposal_id=opportunity.opportunity_id,
            effect_id=effect_id,
            channel=opportunity.channel,
            chat_id=opportunity.chat_id,
            now_ms=int(self._clock_ms()),
            limits=tuple(snapshot.get("reaction_limits") or ()),
            observed_revision=opportunity.observed_revision,
            activation_epoch=opportunity.activation_epoch,
        )
        if not reserved:
            await self._record(opportunity, "skipped", "reaction_budget_exhausted")
            return {"status": "skipped", "reason": "reaction_budget_exhausted"}
        receipt = await self._reactor(
            target_message_id=str(decision.target_message_id),
            emoji=str(decision.emoji),
            channel=opportunity.channel,
            chat_id=opportunity.chat_id,
            effect_id=effect_id,
        )
        # A routed-but-failed reaction is not success: the receipt decides.
        accepted = bool(getattr(receipt, "accepted", False))
        state = str(getattr(receipt, "state", "") or "")
        if accepted and state == "sent":
            await self._ledger.project_transport_accepted(
                opportunity.opportunity_id,
                effect_id=effect_id,
                provider_message_id=_provider_message_id(receipt),
                evidence_kind="transport_receipt",
                evidence_ref=str(getattr(receipt, "attempt_id", "") or effect_id),
                now_ms=int(self._clock_ms()),
            )
        else:
            await self._ledger.release_delivery(
                opportunity.opportunity_id,
                effect_id=effect_id,
                state="failed",
                reason=f"reaction_{state or 'no_receipt'}",
                now_ms=int(self._clock_ms()),
            )
            await self._record(opportunity, "reaction_failed", state or "no_receipt")
            return {"status": "reaction_failed", "reason": state or "no_receipt"}
        await self._record(opportunity, "reaction_submitted", decision.reason)
        return {"status": "reaction_submitted", "effect_id": effect_id}

    async def _run_comment(
        self,
        opportunity: ParticipationOpportunity,
        snapshot: Mapping[str, Any],
        decision: ParticipationDecision,
        context: Mapping[str, Any],
    ) -> dict[str, object]:
        if self._submission is None:
            await self._record(opportunity, "comment_skipped", "no_submission_path")
            return {"status": "comment_skipped", "reason": "no_submission_path"}
        payload_hash = str(snapshot.get("payload_hash") or "")
        reservation = tuple(snapshot.get("comment_limits") or ())
        if not reservation:
            await self._record(opportunity, "comment_skipped", "no_comment_limits")
            return {"status": "comment_skipped", "reason": "no_comment_limits"}
        effect_id = deterministic_effect_id(
            channel=opportunity.channel,
            chat_id=opportunity.chat_id,
            operation="comment",
            proposal_id=opportunity.opportunity_id,
        )
        # The reservation is acquired before generation: capacity is never promised
        # by a draft that cannot be sent.
        reserved = await self._ledger.reserve_delivery(
            proposal_id=opportunity.opportunity_id,
            effect_id=effect_id,
            channel=opportunity.channel,
            chat_id=opportunity.chat_id,
            now_ms=int(self._clock_ms()),
            limits=reservation,
            observed_revision=opportunity.observed_revision,
            activation_epoch=opportunity.activation_epoch,
        )
        if not reserved:
            await self._record(opportunity, "comment_skipped", "comment_budget_exhausted")
            return {"status": "comment_skipped", "reason": "comment_budget_exhausted"}
        try:
            draft = await self._submission.generate_draft(
                opportunity=opportunity,
                decision=decision,
                context=context,
            )
        except Exception as exc:  # noqa: BLE001 - a failed draft releases the hold
            logger.warning(
                "participation_draft_failed chat={} error_type={}",
                opportunity.chat_id,
                type(exc).__name__,
            )
            await self._ledger.release_delivery(
                opportunity.opportunity_id,
                effect_id=effect_id,
                state="failed",
                reason="generation_failed",
                now_ms=int(self._clock_ms()),
            )
            await self._record(opportunity, "generation_failed", "generation_failed")
            return {"status": "generation_failed", "reason": "generation_failed"}
        text = str(draft or "").strip()
        if not text:
            await self._ledger.release_delivery(
                opportunity.opportunity_id,
                effect_id=effect_id,
                state="failed",
                reason="empty_draft",
                now_ms=int(self._clock_ms()),
            )
            await self._record(opportunity, "generation_failed", "empty_draft")
            return {"status": "generation_failed", "reason": "empty_draft"}
        self._count("generated")
        admission = ParticipationAdmission(
            opportunity_id=opportunity.opportunity_id,
            channel=opportunity.channel,
            chat_id=opportunity.chat_id,
            activation_epoch=opportunity.activation_epoch,
            lane=str(snapshot.get("lane") or opportunity.lane),
            observed_revision=opportunity.observed_revision,
            action=decision.action,
            intent=decision.intent,
            purpose=decision.purpose,
            emoji=decision.emoji,
            target_message_id=decision.target_message_id,
            anchor_message_id=decision.anchor_message_id,
        )
        # The reservation now belongs to a submitted effect: local submission alone
        # holds capacity and produces no statement (spec section 9).
        await self._ledger.record_send_attempt(
            opportunity.opportunity_id, effect_id=effect_id, now_ms=int(self._clock_ms())
        )
        outcome = await self._submission.submit(
            admission=admission,
            effect_id=effect_id,
            content=text,
            payload_hash=payload_hash,
        )
        status = str(getattr(outcome, "status", "") or "submitted")
        await self._record(opportunity, f"comment_{status}", decision.reason)
        return {"status": status, "effect_id": effect_id}

    # -- recording ---------------------------------------------------------------------

    async def _record(
        self, opportunity: ParticipationOpportunity, disposition: str, reason: str
    ) -> None:
        try:
            await self._ledger.record_disposition(
                opportunity_id=opportunity.opportunity_id,
                channel=opportunity.channel,
                chat_id=opportunity.chat_id,
                disposition=disposition,
                reason=str(reason)[:160],
                observed_revision=opportunity.observed_revision,
                activation_epoch=opportunity.activation_epoch,
                lane=str(opportunity.lane),
                trigger=str(opportunity.trigger),
                source_ids=opportunity.source_event_ids,
                now_ms=int(self._clock_ms()),
            )
        except Exception as exc:  # noqa: BLE001 - recording must not break decisions
            logger.warning("participation_record_failed error_type={}", type(exc).__name__)


def _provider_message_id(receipt: object) -> str | None:
    transport = getattr(receipt, "transport_receipt", None)
    value = getattr(transport, "provider_message_id", None) if transport else None
    text = str(value or "").strip()
    return text or None


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True, slots=True)
class ParticipationAuthorizationRequest:
    """Everything the local final check needs, resolved without awaiting anything."""

    admission: ParticipationAdmission
    lane: str
    is_paused: str | None
    is_shadow: bool
    feature_enabled: bool
    opted_in: bool
    current_epoch: int
    source_authorized: bool
    effect_id: str
    reservation_state: str | None
    payload_hash: str
    expected_payload_hash: str
    source_principals_authorized: bool = True


class ParticipationEffectAuthorizer:
    """The last local gate before transport for a participation-origin effect.

    The check is deliberately synchronous and must be called with no intervening
    ``await`` before the transport hand-off: a pause that lands between the check and
    the hand-off cannot retroactively stop a request that has already started, and
    pretending otherwise would be a false promise.
    """

    def check(self, request: ParticipationAuthorizationRequest) -> tuple[bool, str]:
        if request.is_shadow or request.lane == "shadow":
            return False, "shadow_lane"
        if not request.feature_enabled:
            return False, "feature_disabled"
        if not request.opted_in:
            return False, "chat_not_opted_in"
        if request.current_epoch != request.admission.activation_epoch:
            return False, "epoch_changed"
        if request.is_paused:
            return False, str(request.is_paused)
        if not request.source_authorized:
            return False, "source_not_authorized"
        if not request.source_principals_authorized:
            return False, "source_principal_not_authorized"
        if request.reservation_state is None:
            return False, "no_reservation"
        if request.reservation_state in {"failed", "cancelled", "expired"}:
            return False, f"reservation_{request.reservation_state}"
        if request.expected_payload_hash and (
            request.payload_hash != request.expected_payload_hash
        ):
            return False, "payload_hash_mismatch"
        return True, "allow"


__all__ = [
    "COUNTERS",
    "ParticipationAdmission",
    "ParticipationAuthorizationRequest",
    "ParticipationBlockedError",
    "ParticipationEffectAuthorizer",
    "ParticipationRuntime",
    "SnapshotProvider",
]
