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
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol, TypeAlias

from loguru import logger

from yeoman_gateway.consciousness.log import deterministic_effect_id
from yeoman_gateway.processing.models import (
    ReactionPayload,
    TextPayload,
    canonical_hash,
    payload_to_mapping,
)
from yeoman_gateway.processing.participation import (
    ParticipationDecision,
    ParticipationDecisionError,
    ParticipationJudge,
    ParticipationOpportunity,
)
from yeoman_gateway.processing.participation_context import (
    ParticipationContextBounds,
    ParticipationDecisionInputs,
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
    "direct_superseded",
    "stale_discarded",
)

_ACTIONS: tuple[str, ...] = ("silence", "react", "comment")
_INTENTS: frozenset[str] = frozenset(("direct", "continue", "initiate"))
_LIMIT_KINDS: frozenset[str] = frozenset(("rolling", "calendar_day"))
_MISSING = object()
_LedgerLimit: TypeAlias = tuple[str, int, int] | tuple[str, int, int, str]


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
    admission_id: str = ""
    source_event_ids: tuple[str, ...] = ()
    source_principals: tuple[tuple[str, str], ...] = ()
    policy_version: str = ""
    policy_hash: str = ""
    arbitration_revision: int = 0
    contribution_type: str = ""
    payload_hash: str = ""
    approval_revision: int = 0


@dataclass(frozen=True, slots=True)
class _FreshCommentState:
    """Fresh trusted state observed while an already-reserved draft is pending."""

    opportunity: ParticipationOpportunity
    snapshot: Mapping[str, Any]
    inputs: ParticipationDecisionInputs
    context: Mapping[str, Any]


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
        direct_work_active: Callable[[str, str], bool] | None = None,
    ) -> None:
        self._judge = judge
        self._context_builder = context_builder
        self._ledger = ledger
        self._snapshot_provider = snapshot_provider
        self._is_paused = is_paused or (lambda channel, chat_id: None)
        self._is_source_allowed = is_source_allowed or (
            lambda channel, chat_id, sources: False
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
        self._direct_work_active = direct_work_active
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
        if self._direct_active(opportunity):
            return self._direct_superseded()
        try:
            raw_snapshot = self._snapshot_provider(
                opportunity.channel,
                opportunity.chat_id,
                epoch=int(opportunity.activation_epoch),
                opportunity=opportunity,
            )
            snapshot = _snapshot_mapping(raw_snapshot)
        except ParticipationBlockedError as blocked:
            self._count("preflight_skipped")
            await self._record(opportunity, "preflight_skipped", blocked.reason)
            return {"status": "skipped", "reason": blocked.reason}
        except Exception:
            self._count("preflight_skipped")
            await self._record(opportunity, "preflight_skipped", "snapshot_error")
            return {"status": "skipped", "reason": "snapshot_error"}

        if self._direct_active(opportunity):
            return self._direct_superseded()

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

        try:
            inputs = await self._decision_inputs(opportunity, snapshot)
            if not any(action != "silence" for action in inputs.allowed_actions):
                raise ParticipationBlockedError("no_feasible_action")
        except ParticipationBlockedError as blocked:
            self._count("preflight_skipped")
            await self._record(opportunity, "preflight_skipped", blocked.reason)
            return {"status": "skipped", "reason": blocked.reason}

        if self._direct_active(opportunity):
            return self._direct_superseded()

        try:
            context = await self._context_builder.build(opportunity, inputs=inputs)
        except ParticipationDecisionError as failure:
            self._count("preflight_skipped")
            await self._record(opportunity, "preflight_skipped", failure.reason)
            return {"status": "skipped", "reason": failure.reason}
        except Exception:
            self._count("preflight_skipped")
            await self._record(opportunity, "preflight_skipped", "context_error")
            return {"status": "skipped", "reason": "context_error"}
        context = await self._hydrate_social_anchor_closures(context)
        inputs, context = self._apply_continuation_candidate(
            opportunity, inputs, context
        )
        if not any(action != "silence" for action in inputs.allowed_actions):
            self._count("preflight_skipped")
            await self._record(opportunity, "preflight_skipped", "no_feasible_action")
            return {"status": "skipped", "reason": "no_feasible_action"}
        if self._direct_active(opportunity):
            return self._direct_superseded()
        attempt_id = f"{opportunity.opportunity_id}:0"
        try:
            charged = await self._ledger.reserve_judge_attempt(
                attempt_id,
                opportunity_id=opportunity.opportunity_id,
                channel=opportunity.channel,
                chat_id=opportunity.chat_id,
                now_ms=int(self._clock_ms()),
                hourly_limit=_snapshot_int(
                    snapshot,
                    "judge_calls_per_hour",
                    "max_unaddressed_judge_calls_per_hour",
                    default=0,
                ),
                min_gap_ms=_snapshot_int(
                    snapshot,
                    "min_gap_seconds",
                    "min_unaddressed_judge_gap_seconds",
                    default=0,
                )
                * 1000,
                # The snapshot value is only a producer hint.  The context pass
                # above has already checked the exact same-chat anchor/source
                # relation, so reserve protection must use that verified result.
                continuation_candidate=bool(inputs.continuation_candidate),
                continuation_reserve=_snapshot_int(
                    inputs.snapshot,
                    "continuation_reserve",
                    "continuation_judge_reserve",
                    default=0,
                ),
            )
        except ParticipationBlockedError as blocked:
            self._count("preflight_skipped")
            await self._record(opportunity, "preflight_skipped", blocked.reason)
            return {"status": "skipped", "reason": blocked.reason}
        if not charged:
            self._count("duplicate_suppressed")
            await self._record(opportunity, "skipped", "attempt_not_available")
            return {"status": "skipped", "reason": "attempt_not_available"}
        self._count("judge_attempted")

        if self._direct_active(opportunity):
            await self._ledger.record_judge_outcome(attempt_id, outcome="direct_superseded")
            return self._direct_superseded()

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

        if self._direct_active(opportunity):
            await self._ledger.record_judge_outcome(attempt_id, outcome="direct_superseded")
            return self._direct_superseded()

        try:
            self._validate_decision(
                decision,
                opportunity=opportunity,
                inputs=inputs,
                context=context,
            )
        except ParticipationDecisionError as failure:
            self._count("judge_failed")
            await self._ledger.record_judge_outcome(attempt_id, outcome=failure.reason)
            await self._record(opportunity, "judge_failed", failure.reason)
            return {"status": "judge_failed", "reason": failure.reason}
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

        if (
            inputs.approval_required
            and decision.action == "comment"
            and decision.intent == "initiate"
        ):
            # Approval persistence/admission belongs to the next wave. Never turn a
            # required approval into an unapproved direct submission here.
            await self._record(opportunity, "comment_skipped", "approval_required")
            return {"status": "comment_skipped", "reason": "approval_required"}

        if decision.action == "silence":
            self._count("deliberate_silence")
            await self._record(opportunity, "decided_silence", decision.reason)
            return {"status": "silence", "intent": decision.intent}

        if decision.action == "react":
            self._count("reaction_selected")
            return await self._run_reaction(opportunity, decision, inputs)

        self._count("comment_selected")
        return await self._run_comment(opportunity, snapshot, decision, context, inputs)

    # -- boundaries --------------------------------------------------------------------

    def _preflight(
        self, opportunity: ParticipationOpportunity, snapshot: Mapping[str, Any]
    ) -> None:
        if not _snapshot_bool(snapshot, "enabled", default=False):
            raise ParticipationBlockedError("feature_disabled")
        if not _snapshot_bool(snapshot, "opted_in", default=False):
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
        # sender's access (spec section 3.1). Missing source evidence is not an allow.
        principals = self._source_principal_values(opportunity)
        for _source_id, sender in principals:
            if not self._is_participant_allowed(opportunity.channel, opportunity.chat_id, sender):
                raise ParticipationBlockedError("source_principal_not_authorized")
        actions = snapshot.get("allowed_actions")
        if isinstance(actions, (list, tuple)) and not set(actions) - {"silence"}:
            # Only silence is feasible: do not spend a provider call to be told that.
            raise ParticipationBlockedError("no_feasible_action")

    def _direct_active(self, opportunity: ParticipationOpportunity) -> bool:
        """Read the durable direct fence without turning failures into an allow."""
        checker = self._direct_work_active
        if checker is None:
            return False
        try:
            return bool(checker(opportunity.channel, opportunity.chat_id))
        except Exception:  # noqa: BLE001 - an unreadable fence must stop work
            return True

    def _direct_superseded(self) -> dict[str, object]:
        self._count("direct_superseded")
        return {"status": "skipped", "reason": "direct_request"}

    async def _hydrate_social_anchor_closures(
        self, context: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Overlay durable social-anchor closure state onto retained anchors."""
        anchors = context.get("anchors")
        checker = getattr(self._ledger, "social_anchor_closed", None)
        if not isinstance(anchors, (tuple, list)) or not callable(checker):
            return context
        channel, chat_id = _context_target(context)
        if not channel or not chat_id:
            return context
        hydrated: list[object] = []
        changed = False
        for raw_anchor in anchors:
            if not isinstance(raw_anchor, Mapping):
                hydrated.append(raw_anchor)
                continue
            anchor = dict(raw_anchor)
            if _anchor_is_closed(anchor):
                hydrated.append(anchor)
                continue
            closed = False
            for anchor_id in _anchor_tokens(anchor):
                try:
                    closed = bool(
                        await checker(
                            channel=channel,
                            chat_id=chat_id,
                            anchor_message_id=anchor_id,
                        )
                    )
                except Exception:  # noqa: BLE001 - unknown closure fails closed
                    closed = True
                if closed:
                    break
            if closed:
                anchor["social_closed"] = True
                changed = True
            hydrated.append(anchor)
        if not changed:
            return context
        result = dict(context)
        result["anchors"] = hydrated
        return result

    def _final_submission_veto(
        self, opportunity: ParticipationOpportunity, snapshot: Mapping[str, Any]
    ) -> str | None:
        """Recheck mutable fences immediately before handing off to submission."""
        if self._direct_active(opportunity):
            return "direct_request"
        try:
            pause = self._is_paused(opportunity.channel, opportunity.chat_id)
        except Exception:  # noqa: BLE001 - unreadable pause state fails closed
            return "pause_state_unavailable"
        if pause:
            return str(pause)
        try:
            source_allowed = self._is_source_allowed(
                opportunity.channel, opportunity.chat_id, opportunity.source_event_ids
            )
        except Exception:  # noqa: BLE001 - unreadable source ACL fails closed
            return "source_not_authorized"
        if not source_allowed:
            return "source_not_authorized"
        try:
            epoch = int(snapshot.get("activation_epoch", opportunity.activation_epoch))
        except (TypeError, ValueError):
            return "epoch_changed"
        if epoch != int(opportunity.activation_epoch):
            return "epoch_changed"
        if str(snapshot.get("lane") or opportunity.lane) == "shadow":
            return "shadow_lane"
        return None

    async def _decision_inputs(
        self,
        opportunity: ParticipationOpportunity,
        snapshot: Mapping[str, Any],
    ) -> ParticipationDecisionInputs:
        """Build one trusted input bundle from the activation snapshot and ledger.

        The snapshot is the only policy source here. The ledger is consulted only for
        current capacity; it remains authoritative when the reservation is acquired.
        Missing or malformed capacity evidence fails closed instead of becoming an
        implicit allow.
        """
        now_ms = int(self._clock_ms())
        comment_limits = _snapshot_limits(snapshot, "comment_limits", "comment")
        reaction_limits = _snapshot_limits(snapshot, "reaction_limits", "reaction")
        initiation_limits = _snapshot_limits(snapshot, "initiation_limits", "initiation")
        if not initiation_limits:
            daily_cap = _snapshot_int(
                snapshot,
                "spontaneity_daily_cap",
                "initiation_daily_cap",
                "daily_cap",
                default=0,
            )
            if daily_cap > 0:
                initiation_limits = (("initiation", daily_cap, 86_400_000, "calendar_day"),)

        comment_remaining = await self._remaining_capacity(
            opportunity, comment_limits, now_ms=now_ms
        )
        reaction_remaining = await self._remaining_capacity(
            opportunity, reaction_limits, now_ms=now_ms
        )
        initiation_remaining = await self._remaining_capacity(
            opportunity, initiation_limits, now_ms=now_ms
        )
        judge_limit = _snapshot_int(
            snapshot,
            "judge_calls_per_hour",
            "max_unaddressed_judge_calls_per_hour",
            default=0,
        )
        judge_remaining = await self._remaining_judge_capacity(
            opportunity, judge_limit, now_ms=now_ms
        )

        allow_initiation = _snapshot_bool(snapshot, "allow_initiation", default=False)
        allow_continuation = _snapshot_bool(snapshot, "allow_continuation", default=False)
        allow_reactions = _snapshot_bool(snapshot, "allow_reactions", default=False)
        spontaneity_enabled = _snapshot_bool(snapshot, "spontaneity_enabled", default=False)
        daily_cap = _snapshot_int(
            snapshot,
            "spontaneity_daily_cap",
            "initiation_daily_cap",
            "daily_cap",
            default=0,
        )
        in_quiet_hours = _in_quiet_hours(snapshot, now_ms)
        contribution_types = _contribution_types(snapshot)
        comment_intents: set[str] = set()
        if (
            allow_continuation
            and _snapshot_bool(snapshot, "continuation_candidate", default=False)
            and comment_remaining > 0
            and contribution_types
        ):
            comment_intents.add("continue")
        if (
            allow_initiation
            and spontaneity_enabled
            and daily_cap > 0
            and initiation_remaining > 0
            and comment_remaining > 0
            and contribution_types
            and not in_quiet_hours
        ):
            comment_intents.add("initiate")
        direct_addressed = _snapshot_bool(snapshot, "direct_addressed", default=False)
        if direct_addressed and comment_remaining > 0:
            comment_intents.add("direct")

        reaction_allowed = allow_reactions and reaction_remaining > 0
        candidate_actions: set[str] = {"silence"}
        if judge_remaining > 0:
            if reaction_allowed:
                candidate_actions.add("react")
            if comment_intents:
                candidate_actions.add("comment")
        requested_actions = _snapshot_tokens(snapshot, "allowed_actions")
        if requested_actions is not None:
            candidate_actions &= set(requested_actions)
        reply_action = _snapshot_value(snapshot, "reply_action")
        if reply_action is not _MISSING and reply_action is not None:
            if reply_action not in {"answer", "react", "silence"}:
                raise ParticipationBlockedError("invalid_snapshot")
            reply_allowed = {
                "answer": {"silence", "comment"},
                "react": {"silence", "react"},
                "silence": {"silence"},
            }[str(reply_action)]
            candidate_actions &= reply_allowed
        candidate_actions.add("silence")
        allowed_actions = tuple(action for action in _ACTIONS if action in candidate_actions)

        requested_intents = _snapshot_tokens(snapshot, "allowed_intents")
        allowed_intents = set(comment_intents)
        if reaction_allowed:
            # Reactions carry a neutral label for model bookkeeping. This label does
            # not grant the corresponding comment intent (checked below).
            allowed_intents.add("continue" if allow_continuation else "initiate")
        if direct_addressed and "direct" in comment_intents:
            allowed_intents.add("direct")
        if requested_intents is not None:
            allowed_intents &= set(requested_intents)

        trusted_snapshot = _snapshot_mapping(snapshot)
        trusted_snapshot.update(
            {
                "allow_initiation": allow_initiation,
                "allow_continuation": allow_continuation,
                "allow_reactions": allow_reactions,
                "spontaneity_enabled": spontaneity_enabled,
                "spontaneity_daily_cap": daily_cap,
                "allowed_contribution_types": _contribution_types(snapshot),
                "comment_allowed_intents": tuple(sorted(comment_intents)),
                "reaction_limits": reaction_limits,
                "comment_limits": comment_limits,
                "initiation_limits": initiation_limits,
            }
        )
        bounds = ParticipationContextBounds(
            window_minutes=_snapshot_positive_int(
                snapshot, "context_window_minutes", default=120
            ),
            max_messages=_snapshot_positive_int(
                snapshot, "context_max_messages", default=40
            ),
            max_anchors=_snapshot_positive_int(
                snapshot, "context_max_anchors", default=10
            ),
        )
        remaining_budgets = (
            ("judge_calls_per_hour", judge_remaining),
            ("initiation_per_day", initiation_remaining),
            ("comments_per_window", comment_remaining),
            ("reactions_per_window", reaction_remaining),
        )
        reservations: list[tuple[str, tuple[_LedgerLimit, ...]]] = []
        if "initiate" in comment_intents:
            reservations.append(("initiate", (*initiation_limits, *comment_limits)))
        if "continue" in comment_intents:
            reservations.append(("continue", comment_limits))
        if "direct" in comment_intents:
            reservations.append(("direct", comment_limits))
        return ParticipationDecisionInputs(
            snapshot=trusted_snapshot,
            bounds=bounds,
            allowed_actions=allowed_actions,
            allowed_intents=frozenset(allowed_intents),
            remaining_budgets=remaining_budgets,
            reservation_limits_by_intent=tuple(reservations),
            approval_required=_snapshot_bool(snapshot, "approval_required", default=False),
            arbitration_revision=_snapshot_nonnegative_int(
                snapshot, "arbitration_revision", default=0
            ),
            current_source_ids=tuple(str(item) for item in opportunity.source_event_ids),
            continuation_candidate=_snapshot_bool(
                snapshot, "continuation_candidate", default=False
            ),
        )

    async def _remaining_capacity(
        self,
        opportunity: ParticipationOpportunity,
        limits: tuple[_LedgerLimit, ...],
        *,
        now_ms: int,
    ) -> int:
        if not limits:
            return 0
        consumed = getattr(self._ledger, "consumed_slots", None)
        if not callable(consumed):
            raise ParticipationBlockedError("capacity_unavailable")
        remaining: list[int] = []
        for category, limit, window_ms, *kind in limits:
            window_kind = kind[0] if kind else "rolling"
            try:
                used = await consumed(
                    channel=opportunity.channel,
                    chat_id=opportunity.chat_id,
                    category=category,
                    now_ms=now_ms,
                    window_ms=window_ms,
                    window_kind=window_kind,
                )
            except Exception as exc:  # noqa: BLE001 - capacity evidence is fail-closed
                raise ParticipationBlockedError("capacity_unavailable") from exc
            try:
                remaining.append(max(0, int(limit) - int(used)))
            except (TypeError, ValueError) as exc:
                raise ParticipationBlockedError("capacity_unavailable") from exc
        return min(remaining)

    async def _remaining_judge_capacity(
        self,
        opportunity: ParticipationOpportunity,
        limit: int,
        *,
        now_ms: int,
    ) -> int:
        if limit <= 0:
            return 0
        attempts_since = getattr(self._ledger, "judge_attempts_since", None)
        if not callable(attempts_since):
            raise ParticipationBlockedError("capacity_unavailable")
        try:
            attempts = await attempts_since(
                channel=opportunity.channel,
                chat_id=opportunity.chat_id,
                since_ms=now_ms - 3_600_000,
            )
        except Exception as exc:  # noqa: BLE001 - capacity evidence is fail-closed
            raise ParticipationBlockedError("capacity_unavailable") from exc
        try:
            count = len(attempts)
        except TypeError as exc:
            raise ParticipationBlockedError("capacity_unavailable") from exc
        return max(0, int(limit) - count)

    def _apply_continuation_candidate(
        self,
        opportunity: ParticipationOpportunity,
        inputs: ParticipationDecisionInputs,
        context: Mapping[str, Any],
    ) -> tuple[ParticipationDecisionInputs, Mapping[str, Any]]:
        """Derive continuation eligibility from retained, exact context evidence.

        The activation snapshot may carry a producer hint, but it cannot turn an
        unrelated source into a protected continuation slot.  This check is local and
        deliberately happens after context construction but before the judge attempt
        is charged.
        """
        candidate_hint = _snapshot_bool(
            inputs.snapshot, "continuation_candidate", default=False
        )
        candidate = candidate_hint and _is_continuation_candidate(
            opportunity,
            context,
        )
        allow_continuation = _snapshot_bool(
            inputs.snapshot, "allow_continuation", default=False
        )
        comment_intents = {
            str(item)
            for item in (
                _snapshot_value(inputs.snapshot, "comment_allowed_intents", ()) or ()
            )
        }
        if candidate and allow_continuation:
            comment_intents.add("continue")
        else:
            comment_intents.discard("continue")

        allowed_intents = set(inputs.allowed_intents)
        if not (candidate and allow_continuation):
            # A reaction may still use ``continue`` as its neutral bookkeeping label;
            # the comment-specific set above remains the authoritative veto.
            if "comment" in inputs.allowed_actions:
                allowed_intents.discard("continue")
        else:
            allowed_intents.add("continue")

        allowed_actions = tuple(inputs.allowed_actions)
        if "comment" in allowed_actions and not comment_intents:
            allowed_actions = tuple(item for item in allowed_actions if item != "comment")
        elif "comment" not in allowed_actions and comment_intents:
            requested = _snapshot_tokens(inputs.snapshot, "allowed_actions")
            reply_action = _snapshot_value(inputs.snapshot, "reply_action")
            if (requested is None or "comment" in requested) and reply_action not in {
                "react",
                "silence",
            }:
                allowed_actions = tuple(
                    sorted({*allowed_actions, "comment"}, key=_ACTIONS.index)
                )

        reservations = [
            (intent, tuple(limits))
            for intent, limits in inputs.reservation_limits_by_intent
            if intent != "continue"
        ]
        if candidate and allow_continuation:
            limits = _snapshot_limits(inputs.snapshot, "comment_limits", "comment")
            if limits:
                reservations.append(("continue", limits))

        trusted_snapshot = _snapshot_mapping(inputs.snapshot)
        trusted_snapshot["continuation_candidate"] = candidate
        trusted_snapshot["comment_allowed_intents"] = tuple(sorted(comment_intents))
        updated_inputs = replace(
            inputs,
            snapshot=trusted_snapshot,
            allowed_actions=allowed_actions,
            allowed_intents=frozenset(allowed_intents),
            reservation_limits_by_intent=tuple(reservations),
            continuation_candidate=candidate,
        )
        updated_context = dict(context)
        updated_context.update(
            {
                "continuation_candidate": candidate,
                "allows_continuation": "continue" in comment_intents,
                "allowed_actions": list(allowed_actions),
                "allowed_intents": sorted(allowed_intents),
            }
        )
        return updated_inputs, updated_context

    def _validate_decision(
        self,
        decision: ParticipationDecision,
        *,
        opportunity: ParticipationOpportunity,
        inputs: ParticipationDecisionInputs,
        context: Mapping[str, Any],
    ) -> None:
        """Re-check the selected action/intent before any reservation or draft."""
        action = str(decision.action)
        intent = str(decision.intent)
        if action == "silence":
            return
        if action not in set(inputs.allowed_actions):
            raise ParticipationDecisionError("invalid_response", detail="action_not_allowed")
        if action == "react":
            if not decision.target_message_id or not decision.emoji:
                raise ParticipationDecisionError("missing_target")
            return
        if intent not in set(inputs.allowed_intents):
            raise ParticipationDecisionError("invalid_response", detail="intent_not_allowed")
        comment_intents = set(
            str(item)
            for item in (_snapshot_value(inputs.snapshot, "comment_allowed_intents", ()) or ())
        )
        if intent not in comment_intents:
            raise ParticipationDecisionError("invalid_response", detail="intent_not_allowed")
        contribution = str(decision.contribution_type or "").strip()
        if contribution not in set(_contribution_types(inputs.snapshot)):
            raise ParticipationDecisionError(
                "invalid_response", detail="contribution_type_not_allowed"
            )
        if intent == "continue":
            if not _has_delivered_anchor(context):
                raise ParticipationDecisionError(
                    "invalid_response", detail="continuation_without_delivered_anchor"
                )
            if not _is_continuation_candidate(
                opportunity,
                context,
            ):
                raise ParticipationDecisionError(
                    "invalid_response", detail="continuation_not_candidate"
                )
            if not _anchor_is_eligible(
                context,
                decision.anchor_message_id,
            ):
                raise ParticipationDecisionError(
                    "invalid_response", detail="continuation_anchor_closed"
                )

    async def _run_reaction(
        self,
        opportunity: ParticipationOpportunity,
        decision: ParticipationDecision,
        inputs: ParticipationDecisionInputs,
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
        try:
            admission = self._build_admission(
                opportunity=opportunity,
                snapshot=inputs.snapshot,
                decision=decision,
                effect_id=effect_id,
                payload=ReactionPayload(
                    message_id=str(decision.target_message_id),
                    emoji=str(decision.emoji),
                ),
            )
        except ParticipationBlockedError as blocked:
            await self._record(opportunity, "reaction_skipped", blocked.reason)
            return {"status": "reaction_skipped", "reason": blocked.reason}
        reserved = await self._ledger.reserve_delivery(
            proposal_id=opportunity.opportunity_id,
            effect_id=effect_id,
            channel=opportunity.channel,
            chat_id=opportunity.chat_id,
            now_ms=int(self._clock_ms()),
            limits=_snapshot_limits(inputs.snapshot, "reaction_limits", "reaction"),
            observed_revision=opportunity.observed_revision,
            activation_epoch=opportunity.activation_epoch,
        )
        if not reserved:
            await self._record(opportunity, "skipped", "reaction_budget_exhausted")
            return {"status": "skipped", "reason": "reaction_budget_exhausted"}
        if self._direct_active(opportunity):
            await self._ledger.release_delivery(
                opportunity.opportunity_id,
                effect_id=effect_id,
                state="failed",
                reason="direct_superseded",
                now_ms=int(self._clock_ms()),
            )
            return self._direct_superseded()
        try:
            receipt = await self._reactor(
                target_message_id=str(decision.target_message_id),
                emoji=str(decision.emoji),
                channel=opportunity.channel,
                chat_id=opportunity.chat_id,
                effect_id=effect_id,
                admission=admission,
            )
        except BaseException as exc:  # noqa: BLE001 - unknown transport work keeps hold
            logger.warning(
                "participation_reaction_unknown chat={} error_type={}",
                opportunity.chat_id,
                type(exc).__name__,
            )
            await self._record(opportunity, "reaction_unknown", type(exc).__name__)
            return {"status": "reaction_unknown", "reason": type(exc).__name__}
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
        elif state in {"failed", "not_executed", "blocked", "cancelled", "expired"}:
            await self._ledger.release_delivery(
                opportunity.opportunity_id,
                effect_id=effect_id,
                state="failed",
                reason=f"reaction_{state or 'no_receipt'}",
                now_ms=int(self._clock_ms()),
            )
            await self._record(opportunity, "reaction_failed", state or "no_receipt")
            return {"status": "reaction_failed", "reason": state or "no_receipt"}
        else:
            # A missing/unknown/in-flight outcome is not evidence that no transport
            # work happened. Keep the reservation for reconciliation instead of making
            # a later retry double-send the same reaction.
            await self._record(opportunity, "reaction_unknown", state or "no_receipt")
            return {"status": "reaction_unknown", "reason": state or "no_receipt"}
        self._close_social_association(opportunity, decision)
        await self._record(opportunity, "reaction_submitted", decision.reason)
        return {"status": "reaction_submitted", "effect_id": effect_id}

    async def _run_comment(
        self,
        opportunity: ParticipationOpportunity,
        snapshot: Mapping[str, Any],
        decision: ParticipationDecision,
        context: Mapping[str, Any],
        inputs: ParticipationDecisionInputs,
    ) -> dict[str, object]:
        if self._submission is None:
            await self._record(opportunity, "comment_skipped", "no_submission_path")
            return {"status": "comment_skipped", "reason": "no_submission_path"}
        reservation = _reservation_for_intent(inputs, decision.intent)
        if not reservation:
            await self._record(opportunity, "comment_skipped", "no_comment_limits")
            return {"status": "comment_skipped", "reason": "no_comment_limits"}
        try:
            _max_reevaluations(snapshot)
        except ParticipationBlockedError as blocked:
            await self._record(opportunity, "comment_skipped", blocked.reason)
            return {"status": "comment_skipped", "reason": blocked.reason}
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
        if not _within_opportunity_deadline(
            opportunity, snapshot, now_ms=int(self._clock_ms())
        ):
            await self._release_comment(opportunity, effect_id, "stale_context")
            await self._record(opportunity, "stale_discarded", "deadline_expired")
            return {"status": "comment_skipped", "reason": "deadline_expired"}
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
        if self._direct_active(opportunity):
            await self._release_comment(opportunity, effect_id, "direct_superseded")
            return self._direct_superseded()
        self._count("generated")
        try:
            fresh = await self._fresh_comment_state(
                opportunity,
                snapshot=snapshot,
                inputs=inputs,
                context=context,
            )
        except ParticipationBlockedError as blocked:
            await self._release_comment(opportunity, effect_id, blocked.reason)
            await self._record(opportunity, "stale_discarded", blocked.reason)
            return {"status": "comment_skipped", "reason": blocked.reason}
        if fresh is not None:
            return await self._reconsider_comment(
                initial_opportunity=opportunity,
                initial_snapshot=snapshot,
                initial_inputs=inputs,
                initial_context=context,
                initial_decision=decision,
                effect_id=effect_id,
                text=text,
                fresh=fresh,
            )
        return await self._submit_comment(
            opportunity=opportunity,
            snapshot=snapshot,
            inputs=inputs,
            decision=decision,
            text=text,
            effect_id=effect_id,
            evaluation_index=0,
        )

    async def _release_comment(self, opportunity: ParticipationOpportunity, effect_id: str, reason: str) -> None:
        await self._ledger.release_delivery(
            opportunity.opportunity_id,
            effect_id=effect_id,
            state="failed",
            reason=str(reason),
            now_ms=int(self._clock_ms()),
        )

    async def _submit_comment(
        self,
        *,
        opportunity: ParticipationOpportunity,
        snapshot: Mapping[str, Any],
        inputs: ParticipationDecisionInputs,
        decision: ParticipationDecision,
        text: str,
        effect_id: str,
        evaluation_index: int,
        deadline_snapshot: Mapping[str, Any] | None = None,
    ) -> dict[str, object]:
        deadline_source = snapshot if deadline_snapshot is None else deadline_snapshot
        if not _within_opportunity_deadline(
            opportunity, deadline_source, now_ms=int(self._clock_ms())
        ):
            await self._release_comment(opportunity, effect_id, "deadline_expired")
            await self._record(opportunity, "stale_discarded", "deadline_expired")
            return {"status": "comment_skipped", "reason": "deadline_expired"}
        payload = TextPayload(text=text)
        try:
            admission = self._build_admission(
                opportunity=opportunity,
                snapshot=snapshot,
                decision=decision,
                effect_id=effect_id,
                payload=payload,
                evaluation_index=evaluation_index,
            )
        except ParticipationBlockedError as blocked:
            await self._release_comment(opportunity, effect_id, blocked.reason)
            await self._record(opportunity, "comment_skipped", blocked.reason)
            return {"status": "comment_skipped", "reason": blocked.reason}
        veto = self._final_submission_veto(opportunity, snapshot)
        if veto == "direct_request":
            await self._release_comment(opportunity, effect_id, "direct_superseded")
            return self._direct_superseded()
        if veto is not None:
            await self._release_comment(opportunity, effect_id, veto)
            await self._record(opportunity, "stale_discarded", veto)
            return {"status": "comment_skipped", "reason": veto}
        outcome = await self._submission.submit(
            admission=admission,
            effect_id=effect_id,
            content=text,
            payload_hash=admission.payload_hash,
        )
        status = str(getattr(outcome, "status", "") or "submitted")
        if status in {"submitted", "transport_accepted", "sent", "delivered"}:
            self._close_social_association(opportunity, decision)
        await self._record(opportunity, f"comment_{status}", decision.reason)
        return {"status": status, "effect_id": effect_id}

    def _close_social_association(
        self,
        opportunity: ParticipationOpportunity,
        decision: ParticipationDecision,
    ) -> None:
        """Retire only the anchor named by the judge, never a task or whole chat."""
        if not decision.closes_exchange or not decision.anchor_message_id:
            return
        token = str(decision.anchor_message_id).strip()
        if not token:
            return
        closer = getattr(self._ledger, "close_social_anchor_sync", None)
        if not callable(closer):
            logger.warning(
                "participation_social_closure_unavailable chat={}", opportunity.chat_id
            )
            return
        try:
            closer(
                channel=str(opportunity.channel),
                chat_id=str(opportunity.chat_id),
                anchor_message_id=token,
                now_ms=int(self._clock_ms()),
            )
        except Exception as exc:  # noqa: BLE001 - delivery already happened; retain evidence
            logger.warning(
                "participation_social_closure_failed chat={} error_type={}",
                opportunity.chat_id,
                type(exc).__name__,
            )

    async def _reconsider_comment(
        self,
        *,
        initial_opportunity: ParticipationOpportunity,
        initial_snapshot: Mapping[str, Any],
        initial_inputs: ParticipationDecisionInputs,
        initial_context: Mapping[str, Any],
        initial_decision: ParticipationDecision,
        effect_id: str,
        text: str,
        fresh: _FreshCommentState,
    ) -> dict[str, object]:
        """Bound stale-draft rejudgement while retaining one delivery reservation."""
        del initial_inputs, initial_context
        # Re-evaluation is bounded by the admission's original setting.  A live
        # policy refresh may reduce that bound, but must not retroactively buy
        # extra provider calls for work that was already admitted.
        max_reevaluations = min(
            _max_reevaluations(initial_snapshot),
            _max_reevaluations(fresh.snapshot),
        )
        original_judge_limit = _snapshot_int(
            initial_snapshot,
            "judge_calls_per_hour",
            "max_unaddressed_judge_calls_per_hour",
            default=0,
        )
        original_min_gap_ms = _snapshot_int(
            initial_snapshot,
            "min_gap_seconds",
            "min_unaddressed_judge_gap_seconds",
            default=0,
        ) * 1000
        original_continuation_reserve = _snapshot_int(
            initial_snapshot,
            "continuation_reserve",
            "continuation_judge_reserve",
            default=0,
        )
        current = fresh
        current_text = text
        current_decision = initial_decision
        initial_signature = _decision_signature(initial_decision)
        replacement_generated = False
        evaluation_index = 1

        while True:
            if evaluation_index > max_reevaluations:
                await self._release_comment(
                    initial_opportunity, effect_id, "context_changed"
                )
                await self._record(
                    current.opportunity, "stale_discarded", "reevaluation_limit"
                )
                return {"status": "comment_skipped", "reason": "context_changed"}
            if not _within_opportunity_deadline(
                initial_opportunity,
                initial_snapshot,
                now_ms=int(self._clock_ms()),
            ):
                await self._release_comment(
                    initial_opportunity, effect_id, "deadline_expired"
                )
                await self._record(
                    current.opportunity, "stale_discarded", "deadline_expired"
                )
                return {"status": "comment_skipped", "reason": "deadline_expired"}
            if self._direct_active(current.opportunity):
                await self._release_comment(
                    initial_opportunity, effect_id, "direct_superseded"
                )
                return self._direct_superseded()

            attempt_id = f"{initial_opportunity.opportunity_id}:{evaluation_index}"
            try:
                charged = await self._ledger.reserve_judge_attempt(
                    attempt_id,
                    opportunity_id=initial_opportunity.opportunity_id,
                    channel=current.opportunity.channel,
                    chat_id=current.opportunity.chat_id,
                    now_ms=int(self._clock_ms()),
                    hourly_limit=min(
                        original_judge_limit,
                        _snapshot_int(
                            current.snapshot,
                            "judge_calls_per_hour",
                            "max_unaddressed_judge_calls_per_hour",
                            default=0,
                        ),
                    ),
                    min_gap_ms=max(
                        original_min_gap_ms,
                        _snapshot_int(
                            current.snapshot,
                            "min_gap_seconds",
                            "min_unaddressed_judge_gap_seconds",
                            default=0,
                        )
                        * 1000,
                    ),
                    continuation_candidate=bool(current.inputs.continuation_candidate),
                    continuation_reserve=min(
                        original_continuation_reserve,
                        _snapshot_int(
                            current.snapshot,
                            "continuation_reserve",
                            "continuation_judge_reserve",
                            default=0,
                        ),
                    ),
                )
            except ParticipationBlockedError as blocked:
                await self._release_comment(initial_opportunity, effect_id, blocked.reason)
                await self._record(
                    current.opportunity, "stale_discarded", blocked.reason
                )
                return {"status": "comment_skipped", "reason": blocked.reason}
            if not charged:
                await self._release_comment(
                    initial_opportunity, effect_id, "reevaluation_budget_exhausted"
                )
                await self._record(
                    current.opportunity,
                    "stale_discarded",
                    "reevaluation_budget_exhausted",
                )
                return {
                    "status": "comment_skipped",
                    "reason": "reevaluation_budget_exhausted",
                }
            self._count("judge_attempted")
            if self._direct_active(current.opportunity):
                await self._ledger.record_judge_outcome(
                    attempt_id, outcome="direct_superseded"
                )
                await self._release_comment(
                    initial_opportunity, effect_id, "direct_superseded"
                )
                return self._direct_superseded()

            try:
                reevaluated = await self._judge.decide(
                    current.opportunity, current.context
                )
            except ParticipationDecisionError as failure:
                await self._ledger.record_judge_outcome(
                    attempt_id, outcome=failure.reason
                )
                self._count("judge_failed")
                await self._release_comment(initial_opportunity, effect_id, failure.reason)
                await self._record(current.opportunity, "judge_failed", failure.reason)
                return {"status": "judge_failed", "reason": failure.reason}
            except Exception as exc:  # noqa: BLE001 - one chat must not stop the queue
                logger.warning(
                    "participation_rejudge_error chat={} error_type={}",
                    current.opportunity.chat_id,
                    type(exc).__name__,
                )
                await self._ledger.record_judge_outcome(
                    attempt_id, outcome="provider_error"
                )
                self._count("judge_failed")
                await self._release_comment(
                    initial_opportunity, effect_id, "provider_error"
                )
                await self._record(current.opportunity, "judge_failed", "provider_error")
                return {"status": "judge_failed", "reason": "provider_error"}

            if self._direct_active(current.opportunity):
                await self._ledger.record_judge_outcome(
                    attempt_id, outcome="direct_superseded"
                )
                await self._release_comment(
                    initial_opportunity, effect_id, "direct_superseded"
                )
                return self._direct_superseded()
            try:
                self._validate_decision(
                    reevaluated,
                    opportunity=current.opportunity,
                    inputs=current.inputs,
                    context=current.context,
                )
            except ParticipationDecisionError as failure:
                await self._ledger.record_judge_outcome(
                    attempt_id, outcome=failure.reason
                )
                self._count("judge_failed")
                await self._release_comment(initial_opportunity, effect_id, failure.reason)
                await self._record(current.opportunity, "judge_failed", failure.reason)
                return {"status": "judge_failed", "reason": failure.reason}
            await self._ledger.record_judge_outcome(
                attempt_id, outcome=reevaluated.action
            )
            if reevaluated.action == "silence":
                self._count("deliberate_silence")
                await self._release_comment(
                    initial_opportunity, effect_id, "reevaluated_silence"
                )
                await self._record(
                    current.opportunity, "stale_discarded", "reevaluated_silence"
                )
                return {"status": "silence", "intent": reevaluated.intent}
            if reevaluated.action != "comment":
                await self._release_comment(
                    initial_opportunity, effect_id, "reevaluated_action_changed"
                )
                await self._record(
                    current.opportunity, "stale_discarded", "reevaluated_action_changed"
                )
                return {"status": "comment_skipped", "reason": "context_changed"}
            if reevaluated.intent != initial_decision.intent:
                # The held categories belong to the original intent. Releasing is the
                # safe atomic transition; a later opportunity may reserve the new set.
                await self._release_comment(
                    initial_opportunity, effect_id, "reservation_intent_changed"
                )
                await self._record(
                    current.opportunity, "stale_discarded", "reservation_intent_changed"
                )
                return {"status": "comment_skipped", "reason": "context_changed"}

            signature = _decision_signature(reevaluated)
            if signature != initial_signature:
                if replacement_generated:
                    await self._release_comment(
                        initial_opportunity, effect_id, "decision_changed_again"
                    )
                    await self._record(
                        current.opportunity, "stale_discarded", "decision_changed_again"
                    )
                    return {"status": "comment_skipped", "reason": "context_changed"}
                try:
                    self._preflight(current.opportunity, current.snapshot)
                except ParticipationBlockedError as blocked:
                    await self._release_comment(
                        initial_opportunity, effect_id, blocked.reason
                    )
                    await self._record(
                        current.opportunity, "stale_discarded", blocked.reason
                    )
                    return {"status": "comment_skipped", "reason": blocked.reason}
                if self._direct_active(current.opportunity):
                    await self._release_comment(
                        initial_opportunity, effect_id, "direct_superseded"
                    )
                    return self._direct_superseded()
                try:
                    replacement = await self._submission.generate_draft(
                        opportunity=current.opportunity,
                        decision=reevaluated,
                        context=current.context,
                    )
                except Exception as exc:  # noqa: BLE001 - release definite failure
                    logger.warning(
                        "participation_replacement_failed chat={} error_type={}",
                        current.opportunity.chat_id,
                        type(exc).__name__,
                    )
                    await self._release_comment(
                        initial_opportunity, effect_id, "generation_failed"
                    )
                    await self._record(
                        current.opportunity, "generation_failed", "generation_failed"
                    )
                    return {"status": "generation_failed", "reason": "generation_failed"}
                current_text = str(replacement or "").strip()
                if not current_text:
                    await self._release_comment(
                        initial_opportunity, effect_id, "empty_draft"
                    )
                    await self._record(current.opportunity, "generation_failed", "empty_draft")
                    return {"status": "generation_failed", "reason": "empty_draft"}
                self._count("generated")
                if self._direct_active(current.opportunity):
                    await self._release_comment(
                        initial_opportunity, effect_id, "direct_superseded"
                    )
                    return self._direct_superseded()
                replacement_generated = True
            current_decision = reevaluated

            try:
                newer = await self._fresh_comment_state(
                    current.opportunity,
                    snapshot=current.snapshot,
                    inputs=current.inputs,
                    context=current.context,
                )
            except ParticipationBlockedError as blocked:
                await self._release_comment(initial_opportunity, effect_id, blocked.reason)
                await self._record(current.opportunity, "stale_discarded", blocked.reason)
                return {"status": "comment_skipped", "reason": blocked.reason}
            if newer is not None:
                if replacement_generated:
                    await self._release_comment(
                        initial_opportunity, effect_id, "context_changed_again"
                    )
                    await self._record(
                        newer.opportunity, "stale_discarded", "context_changed_again"
                    )
                    return {"status": "comment_skipped", "reason": "context_changed"}
                max_reevaluations = min(
                    max_reevaluations, _max_reevaluations(newer.snapshot)
                )
                current = newer
                evaluation_index += 1
                continue
            return await self._submit_comment(
                opportunity=current.opportunity,
                snapshot=current.snapshot,
                inputs=current.inputs,
                decision=current_decision,
                text=current_text,
                effect_id=effect_id,
                evaluation_index=evaluation_index,
                deadline_snapshot=initial_snapshot,
            )

    async def _fresh_comment_state(
        self,
        opportunity: ParticipationOpportunity,
        *,
        snapshot: Mapping[str, Any],
        inputs: ParticipationDecisionInputs,
        context: Mapping[str, Any],
    ) -> _FreshCommentState | None:
        """Read current activation and context revision without opening a new judge."""
        try:
            raw = self._snapshot_provider(
                opportunity.channel,
                opportunity.chat_id,
                epoch=int(opportunity.activation_epoch),
                opportunity=opportunity,
            )
        except Exception as exc:  # noqa: BLE001 - final freshness is fail-closed
            raise ParticipationBlockedError("freshness_unavailable") from exc
        fresh_snapshot = _snapshot_mapping(raw)
        fresh_opportunity = _opportunity_from_snapshot(opportunity, fresh_snapshot)
        self._preflight(fresh_opportunity, fresh_snapshot)
        _max_reevaluations(fresh_snapshot)
        if str(fresh_snapshot.get("lane") or fresh_opportunity.lane) == "shadow":
            raise ParticipationBlockedError("shadow_lane")
        baseline = _revision_token(snapshot, opportunity, context)
        observed = _revision_token(fresh_snapshot, fresh_opportunity, None)
        if not _revision_token_changed(baseline, observed):
            return None
        fresh_inputs = await self._decision_inputs(fresh_opportunity, fresh_snapshot)
        fresh_context = await self._context_builder.build(
            fresh_opportunity, inputs=fresh_inputs
        )
        fresh_context = await self._hydrate_social_anchor_closures(fresh_context)
        fresh_inputs, fresh_context = self._apply_continuation_candidate(
            fresh_opportunity, fresh_inputs, fresh_context
        )
        if not any(action != "silence" for action in fresh_inputs.allowed_actions):
            raise ParticipationBlockedError("no_feasible_action")
        return _FreshCommentState(
            opportunity=fresh_opportunity,
            snapshot=fresh_inputs.snapshot,
            inputs=fresh_inputs,
            context=fresh_context,
        )

    def _source_principal_values(
        self, opportunity: ParticipationOpportunity
    ) -> tuple[tuple[str, str], ...]:
        """Resolve and freeze source principals for the immutable admission."""
        try:
            values = self._source_principals(
                opportunity.channel, opportunity.chat_id, opportunity.source_event_ids
            )
        except Exception as exc:  # noqa: BLE001 - source ACL evidence is mandatory
            raise ParticipationBlockedError("source_principals_unavailable") from exc
        if values is None:
            raise ParticipationBlockedError("source_principals_unavailable")
        source_ids = tuple(
            str(item or "").strip() for item in opportunity.source_event_ids
        )
        if not source_ids or any(not item for item in source_ids):
            raise ParticipationBlockedError("source_events_missing")
        if len(set(source_ids)) != len(source_ids):
            raise ParticipationBlockedError("source_principals_unavailable")
        frozen: list[tuple[str, str]] = []
        for value in values:
            if not isinstance(value, (tuple, list)) or len(value) != 2:
                raise ParticipationBlockedError("source_principals_unavailable")
            source_id = str(value[0] or "").strip()
            sender = str(value[1] or "").strip()
            if not source_id or not sender or source_id not in source_ids:
                raise ParticipationBlockedError("source_principals_unavailable")
            if any(existing[0] == source_id for existing in frozen):
                raise ParticipationBlockedError("source_principals_unavailable")
            frozen.append((source_id, sender))
        if len(frozen) != len(source_ids) or {item[0] for item in frozen} != set(source_ids):
            raise ParticipationBlockedError("source_principals_unavailable")
        return tuple(frozen)

    def _build_admission(
        self,
        *,
        opportunity: ParticipationOpportunity,
        snapshot: Mapping[str, Any],
        decision: ParticipationDecision,
        effect_id: str,
        payload: object,
        evaluation_index: int = 0,
    ) -> ParticipationAdmission:
        source_event_ids = tuple(
            str(item or "").strip() for item in opportunity.source_event_ids
        )
        if not source_event_ids or any(not item for item in source_event_ids):
            raise ParticipationBlockedError("source_events_missing")
        source_principals = self._source_principal_values(opportunity)
        payload_hash = canonical_hash(payload_to_mapping(payload))
        if not payload_hash:
            raise ParticipationBlockedError("payload_hash_missing")
        policy_hash = str(snapshot.get("policy_hash") or "").strip()
        if not policy_hash:
            policy_hash = canonical_hash(dict(snapshot))
        policy_version = str(snapshot.get("policy_version") or "").strip()
        if not policy_version:
            policy_version = f"snapshot:{policy_hash[:16]}"
        try:
            activation_epoch = int(snapshot.get("activation_epoch", opportunity.activation_epoch))
            arbitration_revision = _snapshot_nonnegative_int(
                snapshot, "arbitration_revision", default=0
            )
            approval_revision = _snapshot_nonnegative_int(
                snapshot, "approval_revision", default=0
            )
        except (TypeError, ValueError) as exc:
            raise ParticipationBlockedError("invalid_admission_revision") from exc
        if activation_epoch != int(opportunity.activation_epoch):
            raise ParticipationBlockedError("epoch_changed")
        action = str(decision.action or "").strip()
        intent = str(decision.intent or "").strip()
        if action not in _ACTIONS or not intent:
            raise ParticipationBlockedError("invalid_admission_decision")
        admission_id = f"adm-{effect_id}"
        if int(evaluation_index) > 0:
            admission_id = f"{admission_id}-r{opportunity.observed_revision}-e{int(evaluation_index)}"
        return ParticipationAdmission(
            opportunity_id=opportunity.opportunity_id,
            channel=opportunity.channel,
            chat_id=opportunity.chat_id,
            activation_epoch=activation_epoch,
            lane=str(snapshot.get("lane") or opportunity.lane),
            observed_revision=opportunity.observed_revision,
            action=action,
            intent=intent,
            purpose=str(decision.purpose or ""),
            emoji=decision.emoji,
            target_message_id=decision.target_message_id,
            anchor_message_id=decision.anchor_message_id,
            admission_id=admission_id,
            source_event_ids=source_event_ids,
            source_principals=source_principals,
            policy_version=policy_version,
            policy_hash=policy_hash,
            arbitration_revision=arbitration_revision,
            contribution_type=str(decision.contribution_type or ""),
            payload_hash=payload_hash,
            approval_revision=approval_revision,
        )

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


def _snapshot_value(snapshot: Any, name: str, default: Any = _MISSING) -> Any:
    """Read one canonical snapshot field, including its nested policy object."""
    if isinstance(snapshot, Mapping):
        if name in snapshot:
            return snapshot[name]
        nested = snapshot.get("participation")
    else:
        if hasattr(snapshot, name):
            return getattr(snapshot, name)
        nested = getattr(snapshot, "participation", None)
    if isinstance(nested, Mapping):
        if name in nested:
            return nested[name]
        aliases = {
            "allow_initiation": "allowInitiation",
            "allow_continuation": "allowContinuation",
            "allow_reactions": "allowReactions",
            "max_unaddressed_judge_calls_per_hour": "maxUnaddressedJudgeCallsPerHour",
            "max_reevaluations": "maxReevaluations",
            "opportunity_ttl_seconds": "opportunityTtlSeconds",
        }
        alias = aliases.get(name)
        if alias and alias in nested:
            return nested[alias]
    elif nested is not None and hasattr(nested, name):
        return getattr(nested, name)
    return default


def _snapshot_mapping(snapshot: Any) -> dict[str, Any]:
    if isinstance(snapshot, Mapping):
        return dict(snapshot)
    fields = (
        "enabled",
        "opted_in",
        "invalid_reason",
        "activation_epoch",
        "lane",
        "policy_version",
        "context_window_minutes",
        "context_max_messages",
        "max_reevaluations",
        "opportunity_ttl_seconds",
        "participation",
    )
    return {name: getattr(snapshot, name) for name in fields if hasattr(snapshot, name)}


def _snapshot_sequence(snapshot: Mapping[str, Any], *names: str) -> tuple[str, ...] | None:
    for name in names:
        value = _snapshot_value(snapshot, name)
        if value is _MISSING or value is None:
            continue
        if not isinstance(value, (tuple, list, set, frozenset)):
            return None
        result = tuple(
            str(item).strip() for item in value if str(item).strip()
        )
        return tuple(dict.fromkeys(result))
    return None


def _opportunity_from_snapshot(
    opportunity: ParticipationOpportunity, snapshot: Mapping[str, Any]
) -> ParticipationOpportunity:
    source_ids = _snapshot_sequence(
        snapshot, "current_source_ids", "source_event_ids", "material_source_ids"
    )
    revision = _revision_value(snapshot, default=opportunity.observed_revision)
    if not source_ids:
        source_ids = tuple(opportunity.source_event_ids)
    if revision < opportunity.observed_revision:
        revision = opportunity.observed_revision
    if source_ids == opportunity.source_event_ids and revision == opportunity.observed_revision:
        return opportunity
    return replace(
        opportunity,
        source_event_ids=source_ids,
        observed_revision=revision,
    )


def _revision_value(value: Any, *, default: int) -> int:
    for name in (
        "context_revision",
        "observed_revision",
        "material_revision",
        "source_revision",
        "latest_revision",
    ):
        item = _snapshot_value(value, name)
        if item is _MISSING or item is None:
            continue
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            return int(default)
        return int(item)
    return int(default)


def _revision_token(
    snapshot: Mapping[str, Any] | None,
    opportunity: ParticipationOpportunity,
    context: Mapping[str, Any] | None,
) -> tuple[int, tuple[str, ...]]:
    revision = int(opportunity.observed_revision)
    source_ids = tuple(opportunity.source_event_ids)
    for value in (snapshot, context):
        if value is None:
            continue
        revision = max(revision, _revision_value(value, default=revision))
        supplied = _snapshot_sequence(
            value, "current_source_ids", "source_event_ids", "material_source_ids"
        )
        if supplied:
            source_ids = supplied
    return revision, source_ids


def _revision_token_changed(
    baseline: tuple[int, tuple[str, ...]], observed: tuple[int, tuple[str, ...]]
) -> bool:
    return observed[0] > baseline[0] or observed[1] != baseline[1]


def _max_reevaluations(snapshot: Mapping[str, Any]) -> int:
    value = _snapshot_int(
        snapshot, "max_reevaluations", "maxReevaluations", default=1
    )
    if value > 2:
        raise ParticipationBlockedError("invalid_snapshot")
    return value


def _decision_signature(decision: ParticipationDecision) -> tuple[object, ...]:
    return (
        str(decision.action),
        str(decision.intent),
        str(decision.purpose or ""),
        str(decision.contribution_type or ""),
        str(decision.anchor_message_id or ""),
        str(decision.target_message_id or ""),
    )


def _within_opportunity_deadline(
    opportunity: ParticipationOpportunity,
    snapshot: Mapping[str, Any],
    *,
    now_ms: int,
) -> bool:
    ttl_seconds = _snapshot_int(
        snapshot, "opportunity_ttl_seconds", "ttl_seconds", default=120
    )
    if ttl_seconds <= 0:
        return False
    return int(now_ms) <= int(opportunity.created_at_ms) + ttl_seconds * 1000


def _snapshot_tokens(snapshot: Any, name: str) -> tuple[str, ...] | None:
    value = _snapshot_value(snapshot, name)
    if value is _MISSING or value is None:
        return None
    if not isinstance(value, (tuple, list, set, frozenset)):
        raise ParticipationBlockedError("invalid_snapshot")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ParticipationBlockedError("invalid_snapshot")
        token = item.strip()
        if token not in _ACTIONS and token not in _INTENTS:
            raise ParticipationBlockedError("invalid_snapshot")
        if token not in result:
            result.append(token)
    return tuple(result)


def _snapshot_limits(
    snapshot: Any, name: str, category: str
) -> tuple[_LedgerLimit, ...]:
    value = _snapshot_value(snapshot, name)
    if value is _MISSING or value is None:
        return ()
    if not isinstance(value, tuple):
        raise ParticipationBlockedError("invalid_reservation_limits")
    result: list[_LedgerLimit] = []
    for entry in value:
        if not isinstance(entry, tuple) or len(entry) not in (3, 4):
            raise ParticipationBlockedError("invalid_reservation_limits")
        if entry[0] != category:
            raise ParticipationBlockedError("invalid_reservation_limits")
        if (
            not isinstance(entry[1], int)
            or isinstance(entry[1], bool)
            or not isinstance(entry[2], int)
            or isinstance(entry[2], bool)
            or int(entry[1]) <= 0
            or int(entry[2]) <= 0
        ):
            raise ParticipationBlockedError("invalid_reservation_limits")
        if category == "initiation":
            if len(entry) != 4 or entry[3] != "calendar_day":
                raise ParticipationBlockedError("invalid_reservation_limits")
            result.append((entry[0], int(entry[1]), int(entry[2]), entry[3]))
            continue
        if len(entry) == 4 and entry[3] != "rolling":
            raise ParticipationBlockedError("invalid_reservation_limits")
        if len(entry) == 3:
            result.append((entry[0], int(entry[1]), int(entry[2])))
        else:
            result.append((entry[0], int(entry[1]), int(entry[2]), entry[3]))
    return tuple(result)


def _snapshot_int(snapshot: Any, *names: str, default: int) -> int:
    for name in names:
        value = _snapshot_value(snapshot, name)
        if value is _MISSING or value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ParticipationBlockedError("invalid_snapshot")
        return int(value)
    return int(default)


def _snapshot_positive_int(snapshot: Any, name: str, *, default: int) -> int:
    value = _snapshot_int(snapshot, name, default=default)
    if value <= 0:
        raise ParticipationBlockedError("invalid_snapshot")
    return value


def _snapshot_nonnegative_int(snapshot: Any, name: str, *, default: int) -> int:
    return _snapshot_int(snapshot, name, default=default)


def _snapshot_bool(snapshot: Any, name: str, *, default: bool) -> bool:
    value = _snapshot_value(snapshot, name)
    if value is _MISSING or value is None:
        return bool(default)
    if not isinstance(value, bool):
        raise ParticipationBlockedError("invalid_snapshot")
    return value


def _contribution_types(snapshot: Any) -> tuple[str, ...]:
    value = _snapshot_value(snapshot, "allowed_contribution_types")
    if value is _MISSING or value is None:
        value = _snapshot_value(snapshot, "spontaneity_allowed_actions")
    if value is _MISSING or value is None:
        return ()
    if not isinstance(value, (tuple, list, set, frozenset)):
        raise ParticipationBlockedError("invalid_snapshot")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ParticipationBlockedError("invalid_snapshot")
        if item.strip() not in result:
            result.append(item.strip())
    return tuple(result)


def _in_quiet_hours(snapshot: Any, now_ms: int) -> bool:
    start = _snapshot_value(snapshot, "spontaneity_quiet_hours_start")
    end = _snapshot_value(snapshot, "spontaneity_quiet_hours_end")
    if start in (_MISSING, None, "") or end in (_MISSING, None, ""):
        return False
    try:
        start_minutes = _clock_minutes(str(start))
        end_minutes = _clock_minutes(str(end))
    except ValueError:
        return True
    current = datetime.fromtimestamp(int(now_ms) / 1000, UTC)
    current_minutes = current.hour * 60 + current.minute
    if start_minutes == end_minutes:
        return True
    if start_minutes < end_minutes:
        return start_minutes <= current_minutes < end_minutes
    return current_minutes >= start_minutes or current_minutes < end_minutes


def _clock_minutes(value: str) -> int:
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ValueError(value)
    hours, minutes = (int(part) for part in parts)
    if not 0 <= hours <= 23 or not 0 <= minutes <= 59:
        raise ValueError(value)
    return hours * 60 + minutes


_REFERENCE_KEYS: tuple[str, ...] = (
    "reply_to_message_id",
    "reply_to",
    "replyToMessageId",
    "quoted_message_id",
    "quoted_provider_message_id",
    "reference_message_id",
    "referenced_message_id",
    "anchor_message_id",
    "reply_to_effect_id",
    "quoted_effect_id",
)


def _context_target(context: Mapping[str, Any]) -> tuple[str, str]:
    return (
        str(context.get("channel") or "").strip(),
        str(context.get("chat_id") or "").strip(),
    )


def _anchor_tokens(anchor: Mapping[str, Any]) -> frozenset[str]:
    return frozenset(
        token
        for token in (
            str(anchor.get("provider_message_id") or "").strip(),
            str(anchor.get("effect_id") or "").strip(),
        )
        if token
    )


def _anchor_is_closed(anchor: Mapping[str, Any]) -> bool:
    for name in (
        "closed",
        "exchange_closed",
        "association_closed",
        "social_closed",
        "retired",
        "replaced",
    ):
        if anchor.get(name) is True:
            return True
    return any(
        str(anchor.get(name) or "").strip()
        for name in ("closed_at_ms", "closed_at", "replaced_by")
    )


def _anchor_is_same_target(
    anchor: Mapping[str, Any], context: Mapping[str, Any]
) -> bool:
    target_channel, target_chat = _context_target(context)
    anchor_channel = str(anchor.get("channel") or "").strip()
    anchor_chat = str(anchor.get("chat_id") or "").strip()
    if anchor_channel or anchor_chat:
        return bool(
            target_channel
            and target_chat
            and anchor_channel == target_channel
            and anchor_chat == target_chat
        )
    # A context builder that returned an anchor without its repeated target still
    # carries exact query provenance in its top-level target.  Without either proof,
    # an arbitrary anchor is not eligible for a protected continuation.
    return bool(target_channel and target_chat)


def _anchor_is_eligible(
    context: Mapping[str, Any],
    anchor_id: str | None,
) -> bool:
    anchors = context.get("anchors")
    if not isinstance(anchors, (tuple, list)):
        return False
    requested = str(anchor_id or "").strip()
    for raw_anchor in anchors:
        if not isinstance(raw_anchor, Mapping):
            continue
        anchor = raw_anchor
        tokens = _anchor_tokens(anchor)
        if (
            str(anchor.get("delivery_state") or "") != "delivered"
            or not tokens
            or (requested and requested not in tokens)
            or not _anchor_is_same_target(anchor, context)
            or _anchor_is_closed(anchor)
        ):
            continue
        return True
    return False


def _has_delivered_anchor(context: Mapping[str, Any]) -> bool:
    return _anchor_is_eligible(context, None)


def _reference_tokens(source: Mapping[str, Any]) -> frozenset[str]:
    values: set[str] = set()
    for key in _REFERENCE_KEYS:
        value = source.get(key)
        if isinstance(value, Mapping):
            value = value.get("message_id") or value.get("provider_message_id") or value.get(
                "id"
            )
        token = str(value or "").strip()
        if token:
            values.add(token)
    metadata = source.get("metadata")
    if isinstance(metadata, Mapping):
        values.update(_reference_tokens(metadata))
    return frozenset(values)


def _source_id(row: Mapping[str, Any]) -> str:
    return str(row.get("event_id") or row.get("message_id") or "").strip()


def _source_sender(row: Mapping[str, Any]) -> str:
    return str(
        row.get("sender_id") or row.get("sender") or row.get("speaker") or ""
    ).strip()


def _is_bot_sender(sender: str) -> bool:
    token = str(sender or "").strip().casefold()
    return token in {"assistant", "arvid", "bot", "service:speakup"} or token.endswith(
        ":bot"
    )


def _context_time_ms(row: Mapping[str, Any]) -> int | None:
    value = row.get("timestamp")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        return int(number * 1000 if abs(number) < 100_000_000_000 else number)
    value = row.get("delivered_at_ms")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    return None


def _has_replacing_foreign_exchange(
    source: Mapping[str, Any],
    anchor: Mapping[str, Any],
    context: Mapping[str, Any],
    *,
    source_sender: str,
) -> bool:
    markers = (
        "foreign_exchange",
        "intervening_foreign_exchange",
        "replacing_foreign_exchange",
        "exchange_replaced",
        "replaced_exchange",
        "replaces_anchor",
    )
    if any(item.get(name) is True for item in (source, anchor, context) for name in markers):
        return True
    messages = context.get("messages")
    if isinstance(messages, (tuple, list)):
        if any(
            isinstance(item, Mapping)
            and any(item.get(name) is True for name in markers)
            for item in messages
        ):
            return True
    anchor_time = _context_time_ms(anchor)
    source_time = _context_time_ms(source)
    if anchor_time is None or source_time is None or source_time < anchor_time:
        return False
    messages = context.get("messages")
    if not isinstance(messages, (tuple, list)):
        return False
    for raw_message in messages:
        if not isinstance(raw_message, Mapping) or _source_id(raw_message) == _source_id(source):
            continue
        message_time = _context_time_ms(raw_message)
        if message_time is None or not anchor_time < message_time <= source_time:
            continue
        sender = _source_sender(raw_message)
        if sender and sender != source_sender and not _is_bot_sender(sender):
            return True
    return False


def _is_continuation_candidate(
    opportunity: ParticipationOpportunity,
    context: Mapping[str, Any],
) -> bool:
    """Return whether this source has cheap, exact evidence for protected reserve use."""
    anchors = context.get("anchors")
    messages = context.get("messages")
    if not isinstance(anchors, (tuple, list)) or not isinstance(messages, (tuple, list)):
        return False
    eligible_anchors = [
        anchor
        for anchor in anchors
        if isinstance(anchor, Mapping)
        and _anchor_is_eligible(
            {**context, "anchors": [anchor]},
            None,
        )
    ]
    if not eligible_anchors:
        return False
    source_ids = {str(item).strip() for item in opportunity.source_event_ids if str(item).strip()}
    source_rows = [
        row
        for row in messages
        if isinstance(row, Mapping) and _source_id(row) in source_ids
    ]
    if not source_rows:
        return False
    target_channel, target_chat = _context_target(context)
    for source in source_rows:
        source_channel = str(source.get("channel") or "").strip()
        source_chat = str(source.get("chat_id") or "").strip()
        if source_channel or source_chat:
            if not (
                target_channel
                and target_chat
                and source_channel == target_channel
                and source_chat == target_chat
            ):
                continue
        sender = _source_sender(source)
        if not sender or _is_bot_sender(sender):
            continue
        references = _reference_tokens(source)
        for anchor in eligible_anchors:
            if references & _anchor_tokens(anchor):
                # An exact same-chat bot reference is sufficient even when the text is
                # longer than the short-message heuristic.
                return True
        text = str(source.get("text") or "").strip()
        if not text or len(text) > 120 or references:
            continue
        for anchor in eligible_anchors:
            anchor_time = _context_time_ms(anchor)
            source_time = _context_time_ms(source)
            if (
                anchor_time is not None
                and source_time is not None
                and source_time < anchor_time
            ):
                continue
            if not _has_replacing_foreign_exchange(
                source,
                anchor,
                context,
                source_sender=sender,
            ):
                return True
    return False


def _reservation_for_intent(
    inputs: ParticipationDecisionInputs, intent: str
) -> tuple[_LedgerLimit, ...]:
    for candidate, limits in inputs.reservation_limits_by_intent:
        if candidate == intent:
            return tuple(limits)
    return ()


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
    source_principals_authorized: bool = False


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
        if request.lane != "production" or getattr(request.admission, "lane", None) != "production":
            return False, "admission_lane_mismatch"
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
        if request.source_principals_authorized is not True:
            return False, "source_principal_not_authorized"
        if request.reservation_state is None:
            return False, "no_reservation"
        if request.reservation_state != "submitted":
            if request.reservation_state not in {"failed", "cancelled", "expired"}:
                return False, "reservation_not_submitted"
            return False, f"reservation_{request.reservation_state}"
        if not request.payload_hash or not request.expected_payload_hash:
            return False, "payload_hash_missing"
        if request.payload_hash != request.expected_payload_hash:
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
