"""One decision, then exactly the work it requires (04.2 call-count acceptance).

Real temporary ledger, real snapshot resolution from policy, controlled judge and
generator. The point of these tests is the call-count table and the boundaries, not
model quality.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from yeoman_gateway.consciousness.log import SpeakupLog, deterministic_effect_id
from yeoman_gateway.policy.engine import PolicyEngine
from yeoman_gateway.policy.schema import PolicyConfig
from yeoman_gateway.processing.models import TextPayload, TransportReceipt, payload_hash
from yeoman_gateway.processing.participation import (
    ParticipationDecision,
    ParticipationDecisionError,
    ParticipationOpportunity,
)
from yeoman_gateway.processing.participation_runtime import ParticipationRuntime
from yeoman_shared.config.schema import ProcessingConfig

CHANNEL = "whatsapp"
CHAT = "group@g.us"
EMOJI = "\N{THUMBS UP SIGN}"
NOW_MS = 1_800_000_000_000


def _opportunity(*sources: str, lane: str = "production") -> ParticipationOpportunity:
    return ParticipationOpportunity(
        opportunity_id="opp-1",
        channel=CHANNEL,
        chat_id=CHAT,
        trigger="inbound",
        source_event_ids=sources or ("m1",),
        observed_revision=3,
        activation_epoch=1,
        created_at_ms=NOW_MS,
        lane=lane,
    )


class _Judge:
    def __init__(self, decision: ParticipationDecision | BaseException) -> None:
        self._decision = decision
        self.calls = 0

    async def decide(self, opportunity, context):
        del opportunity, context
        self.calls += 1
        if isinstance(self._decision, BaseException):
            raise self._decision
        return self._decision


class _Context:
    def __init__(self) -> None:
        self.calls = 0
        self.inputs = None

    async def build(self, opportunity, *, inputs):
        self.calls += 1
        self.inputs = inputs
        source_id = (
            str(opportunity.source_event_ids[-1])
            if opportunity.source_event_ids
            else "m1"
        )
        snapshot = inputs.snapshot
        contribution_types = (
            snapshot.get("allowed_contribution_types", ())
            if isinstance(snapshot, dict)
            else ()
        )
        return {
            "channel": CHANNEL,
            "chat_id": CHAT,
            "messages": [
                {
                    "event_id": source_id,
                    "sender_id": "anna@s.whatsapp.net",
                    "text": "a short synthetic reply",
                    "timestamp": NOW_MS,
                    "channel": CHANNEL,
                    "chat_id": CHAT,
                }
            ],
            "anchors": [
                {
                    "provider_message_id": "prov-1",
                    "delivery_state": "delivered",
                    "delivered_at_ms": NOW_MS - 1_000,
                    "channel": CHANNEL,
                    "chat_id": CHAT,
                    "closed": False,
                }
            ],
            "allowed_actions": list(inputs.allowed_actions),
            "allowed_intents": list(inputs.allowed_intents),
            "allowed_contribution_types": list(contribution_types),
        }


@dataclass
class _Receipt:
    effect_id: str
    state: str = "sent"
    accepted: bool = True
    attempt_id: str = "attempt-1"
    transport_receipt: object | None = None


@dataclass
class _Submission:
    calls: int = 0
    drafts: list[str] = field(default_factory=list)
    status: str = "submitted"

    async def generate_draft(self, *, opportunity, decision, context):
        del opportunity, decision, context
        self.calls += 1
        return "a synthetic draft"

    async def submit(self, *, admission, effect_id, content, payload_hash):
        del admission, effect_id, content, payload_hash
        return _Receipt(effect_id="e1", state=self.status)


@dataclass
class _ApprovalSubmission(_Submission):
    approval_calls: list[dict[str, object]] = field(default_factory=list)

    async def queue_approval(
        self,
        *,
        opportunity,
        decision,
        admission,
        effect_id,
        content,
        snapshot,
    ):
        self.approval_calls.append(
            {
                "opportunity": opportunity,
                "decision": decision,
                "admission": admission,
                "effect_id": effect_id,
                "content": content,
                "snapshot": snapshot,
            }
        )
        return {"status": "awaiting_approval", "effect_id": effect_id}


class _Reactor:
    def __init__(self, *, state: str = "sent") -> None:
        self.calls: list[dict[str, object]] = []
        self._state = state

    async def __call__(self, **kwargs: object):
        self.calls.append(dict(kwargs))
        return _Receipt(
            effect_id=str(kwargs.get("effect_id") or ""),
            state=self._state,
            accepted=self._state == "sent",
            transport_receipt=(
                TransportReceipt(
                    channel=CHANNEL,
                    chat_id=CHAT,
                    provider_message_id="reaction-provider-1",
                    confirmed_ms=NOW_MS,
                )
                if self._state == "sent"
                else None
            ),
        )


def _policy(*, opted_in: bool = True, participation: dict | None = None) -> PolicyConfig:
    block: dict[str, object] = {"enabled": opted_in}
    if participation:
        block.update(participation)
    return PolicyConfig.model_validate(
        {
            "channels": {
                CHANNEL: {
                    "chats": {
                        CHAT: {
                            "whoCanTalk": {"mode": "everyone"},
                            "participation": block,
                            "spontaneity": {
                                "enabled": True,
                                "profile": "balanced",
                                "dailyCap": 10,
                                "allowedActions": ["observation", "light_humor"],
                            },
                        }
                    }
                }
            }
        }
    )


def _processing(*, enabled: bool = True, shadow: bool = False) -> ProcessingConfig:
    return ProcessingConfig.model_validate(
        {
            "enabled": enabled,
            "participation": {
                "enabled": enabled,
                "shadow": shadow,
                "judgeRoute": "participation.judge" if enabled else "",
            }
        }
    )


def _runtime(
    tmp_path: Path,
    *,
    decision: ParticipationDecision | BaseException,
    opted_in: bool = True,
    enabled: bool = True,
    shadow: bool = False,
    paused: str | None = None,
    source_allowed: bool = True,
    reactor: _Reactor | None = None,
    submission: _Submission | None = None,
    policy_participation: dict | None = None,
    processing_shadowed: bool = False,
    approval_required: bool = False,
    reply_action: str | None = None,
    writer_available: bool = True,
    snapshot_overrides: dict[str, object] | None = None,
):
    log = SpeakupLog(tmp_path / "speakups.db")
    engine = PolicyEngine(
        _policy(opted_in=opted_in, participation=policy_participation), workspace=tmp_path
    )
    config = _processing(enabled=enabled, shadow=shadow)

    def _snapshot(
        channel: str, chat_id: str, *, epoch: int, opportunity: object | None = None
    ) -> dict[str, object]:
        resolved = engine.resolve_participation_snapshot(
            channel,
            chat_id,
            processing_config=config,
            activation_epoch=epoch,
            managed=True,
            processing_shadowed=processing_shadowed,
        )
        reaction_limit = int(resolved.participation.max_reactions_per_window)
        comment_limit = int(resolved.participation.max_unsolicited_comments_per_window)
        window_ms = int(resolved.participation.comment_window_minutes) * 60_000
        policy = engine.resolve_policy(channel, chat_id)
        initiation_daily_cap = int(policy.spontaneity_daily_cap or 0)
        return {
            "enabled": resolved.enabled,
            "opted_in": resolved.opted_in,
            "invalid_reason": resolved.invalid_reason,
            "activation_epoch": resolved.activation_epoch,
            "lane": "shadow" if resolved.shadow else "production",
            "judge_calls_per_hour": int(
                resolved.participation.max_unaddressed_judge_calls_per_hour
            ),
            "min_gap_seconds": int(
                resolved.participation.min_unaddressed_judge_gap_seconds
            ),
            "continuation_reserve": int(resolved.participation.continuation_judge_reserve),
            "context_window_minutes": int(resolved.context_window_minutes),
            "context_max_messages": int(resolved.context_max_messages),
            "allow_initiation": bool(resolved.participation.allow_initiation),
            "allow_continuation": bool(resolved.participation.allow_continuation),
            "allow_reactions": bool(resolved.participation.allow_reactions),
            "spontaneity_enabled": bool(policy.spontaneity_enabled),
            "spontaneity_daily_cap": initiation_daily_cap,
            "spontaneity_allowed_actions": tuple(policy.spontaneity_allowed_actions or ()),
            "initiation_limits": (
                ("initiation", initiation_daily_cap, 86_400_000, "calendar_day"),
            ) if initiation_daily_cap > 0 else (),
            "allowed_contribution_types": tuple(policy.spontaneity_allowed_actions or ()),
            "approval_required": approval_required,
            "arbitration_revision": 0,
            **({"reply_action": reply_action} if reply_action is not None else {}),
            "continuation_candidate": bool(
                opportunity and getattr(opportunity, "source_event_ids", ())
            ),
            "reaction_limits": (("reaction", reaction_limit, window_ms),)
            if reaction_limit > 0
            else (),
            "comment_limits": (("comment", comment_limit, window_ms),)
            if comment_limit > 0
            else (),
            "payload_hash": "hash-1",
            **(snapshot_overrides or {}),
        }

    judge = _Judge(decision)
    context = _Context()
    runtime = ParticipationRuntime(
        judge=judge,  # type: ignore[arg-type]
        context_builder=context,
        ledger=log,
        snapshot_provider=_snapshot,
        is_paused=lambda channel, chat_id: paused,
        is_source_allowed=lambda channel, chat_id, sources: source_allowed,
        source_principals=lambda channel, chat_id, sources: tuple(
            (str(source), "anna@s.whatsapp.net") for source in sources
        ),
        submission=submission if submission is not None else _Submission(),
        reactor=reactor if reactor is not None else _Reactor(),
        writer_available=writer_available,
        clock_ms=lambda: NOW_MS,
    )
    return runtime, judge, context, log


SILENCE = ParticipationDecision(action="silence", intent="initiate", reason="nothing to add")
REACT = ParticipationDecision(
    action="react",
    intent="continue",
    reason="ack",
    emoji=EMOJI,
    target_message_id="m1",
    anchor_message_id="prov-1",
)
COMMENT = ParticipationDecision(
    action="comment",
    intent="initiate",
    reason="useful",
    purpose="answer the goalkeeper question",
    contribution_type="observation",
    target_message_id="m1",
)


@pytest.mark.asyncio
async def test_hard_refusal_runs_no_judge_and_no_effect(tmp_path: Path) -> None:
    for kwargs, reason in (
        ({"enabled": False}, "feature_disabled"),
        ({"opted_in": False}, "chat_not_opted_in"),
        ({"paused": "paused_global"}, "paused_global"),
        ({"source_allowed": False}, "source_not_authorized"),
        ({"processing_shadowed": True}, "processing_shadow_conflict"),
    ):
        runtime, judge, context, log = _runtime(tmp_path, decision=SILENCE, **kwargs)
        result = await runtime.evaluate_participation(_opportunity())
        assert result["status"] == "skipped", kwargs
        assert result["reason"] == reason, kwargs
        assert judge.calls == 0
        assert context.calls == 0
        log.close()


@pytest.mark.asyncio
async def test_silence_costs_one_judge_and_nothing_else(tmp_path: Path) -> None:
    submission = _Submission()
    reactor = _Reactor()
    runtime, judge, context, log = _runtime(
        tmp_path, decision=SILENCE, submission=submission, reactor=reactor
    )
    result = await runtime.evaluate_participation(_opportunity())
    assert result == {"status": "silence", "intent": "initiate"}
    assert judge.calls == 1
    assert context.calls == 1
    assert submission.calls == 0
    assert reactor.calls == []
    assert runtime.counters()["deliberate_silence"] == 1
    state = await log.disposition("opp-1")
    assert state is not None and state["disposition"] == "decided_silence"
    log.close()


@pytest.mark.asyncio
async def test_reaction_is_one_judge_and_one_reaction_effect(tmp_path: Path) -> None:
    submission = _Submission()
    reactor = _Reactor()
    runtime, judge, _context, log = _runtime(
        tmp_path, decision=REACT, submission=submission, reactor=reactor
    )
    result = await runtime.evaluate_participation(_opportunity())
    assert result["status"] == "reaction_submitted"
    assert judge.calls == 1
    assert submission.calls == 0
    assert len(reactor.calls) == 1
    call = reactor.calls[0]
    assert call["target_message_id"] == "m1"
    assert call["emoji"] == EMOJI
    admission = call["admission"]
    assert admission.admission_id == f"adm-{call['effect_id']}"
    assert admission.source_event_ids == ("m1",)
    assert admission.source_principals == (("m1", "anna@s.whatsapp.net"),)
    assert admission.payload_hash
    effect_id = str(call["effect_id"])
    assert effect_id == deterministic_effect_id(
        channel=CHANNEL, chat_id=CHAT, operation="reaction", proposal_id="opp-1"
    )
    record = await log.delivery_record(proposal_id="opp-1", effect_id=effect_id)
    assert record is not None and record["delivery_state"] == "transport_accepted"
    assert await log.delivered_reservation_rows(
        channel=CHANNEL, chat_id=CHAT, since_ms=0, limit=10
    ) == []
    log.close()


@pytest.mark.asyncio
async def test_reaction_requires_a_receipt_not_a_handled_boolean(tmp_path: Path) -> None:
    """A routed-but-failed reaction consumes no success metric and frees the slot."""
    reactor = _Reactor(state="failed")
    runtime, _judge, _context, log = _runtime(tmp_path, decision=REACT, reactor=reactor)
    result = await runtime.evaluate_participation(_opportunity())
    assert result["status"] == "reaction_failed"
    record = await log.delivery_record(
        proposal_id="opp-1",
        effect_id=deterministic_effect_id(
            channel=CHANNEL, chat_id=CHAT, operation="reaction", proposal_id="opp-1"
        ),
    )
    assert record is not None and record["delivery_state"] == "failed"
    assert await log.consumed_slots(
        channel=CHANNEL, chat_id=CHAT, category="reaction", now_ms=NOW_MS, window_ms=1_800_000
    ) == 0
    log.close()


@pytest.mark.asyncio
async def test_comment_is_one_judge_one_generation_one_effect(tmp_path: Path) -> None:
    submission = _Submission()
    runtime, judge, _context, log = _runtime(
        tmp_path, decision=COMMENT, submission=submission
    )
    result = await runtime.evaluate_participation(_opportunity())
    assert result["status"] == "submitted"
    assert judge.calls == 1
    assert submission.calls == 1
    record = await log.delivery_record(proposal_id="opp-1", effect_id=str(result["effect_id"]))
    # The fake submission does not own the shared effect router, so no early runtime
    # attempt projection is fabricated here; the real adapter records ``submitted``
    # after its atomic effect enqueue.
    assert record is not None and record["attempt_state"] == "unsubmitted"
    assert await log.consumed_slots(
        channel=CHANNEL, chat_id=CHAT, category="comment", now_ms=NOW_MS, window_ms=1_800_000
    ) == 1
    log.close()


@pytest.mark.asyncio
async def test_writer_unavailable_removes_only_comment(tmp_path: Path) -> None:
    """A missing writer must not disable an otherwise permitted reaction."""
    runtime, _judge, context, log = _runtime(
        tmp_path,
        decision=REACT,
        writer_available=False,
        reply_action="answer",
    )

    result = await runtime.evaluate_participation(_opportunity())

    assert result["status"] == "reaction_submitted"
    assert context.inputs is not None
    assert context.inputs.allowed_actions == ("silence", "react")
    assert context.inputs.snapshot["writer_unavailable"] is True
    log.close()


@pytest.mark.asyncio
async def test_writer_unavailable_is_visible_when_comment_is_the_only_effect(
    tmp_path: Path,
) -> None:
    runtime, judge, context, log = _runtime(
        tmp_path,
        decision=COMMENT,
        writer_available=False,
        policy_participation={"enabled": True, "allowReactions": False},
    )

    result = await runtime.evaluate_participation(_opportunity())

    assert result == {"status": "skipped", "reason": "writer_unavailable"}
    assert judge.calls == 0
    assert context.calls == 0
    log.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["provider_error", "timeout"])
async def test_classified_draft_failure_releases_reservation(
    tmp_path: Path, reason: str
) -> None:
    from yeoman_gateway.processing.participation_runtime import ParticipationDraftError

    class FailingSubmission(_Submission):
        async def generate_draft(self, *, opportunity, decision, context):
            del opportunity, decision, context
            self.calls += 1
            raise ParticipationDraftError(reason)

    submission = FailingSubmission()
    runtime, _judge, _context, log = _runtime(
        tmp_path, decision=COMMENT, submission=submission
    )

    result = await runtime.evaluate_participation(_opportunity())

    assert result == {"status": "generation_failed", "reason": reason}
    assert submission.calls == 1
    assert await log.pending_delivery_reservations() == []
    record = await log.delivery_record(
        proposal_id="opp-1",
        effect_id=deterministic_effect_id(
            channel=CHANNEL, chat_id=CHAT, operation="comment", proposal_id="opp-1"
        ),
    )
    assert record is not None and record["delivery_state"] == "failed"
    disposition = await log.disposition("opp-1")
    assert disposition is not None and disposition["reason"] == reason
    log.close()


@pytest.mark.asyncio
async def test_empty_draft_remains_distinct_and_releases_reservation(tmp_path: Path) -> None:
    class EmptySubmission(_Submission):
        async def generate_draft(self, *, opportunity, decision, context):
            del opportunity, decision, context
            self.calls += 1
            return ""

    submission = EmptySubmission()
    runtime, _judge, _context, log = _runtime(
        tmp_path, decision=COMMENT, submission=submission
    )

    result = await runtime.evaluate_participation(_opportunity())

    assert result == {"status": "generation_failed", "reason": "empty_draft"}
    assert await log.pending_delivery_reservations() == []
    log.close()


@pytest.mark.asyncio
async def test_judge_failure_records_a_failure_and_generates_nothing(tmp_path: Path) -> None:
    submission = _Submission()
    runtime, judge, _context, log = _runtime(
        tmp_path,
        decision=ParticipationDecisionError("timeout"),
        submission=submission,
    )
    result = await runtime.evaluate_participation(_opportunity())
    assert result == {"status": "judge_failed", "reason": "timeout"}
    assert judge.calls == 1
    assert submission.calls == 0
    state = await log.disposition("opp-1")
    assert state is not None and state["disposition"] == "judge_failed"
    log.close()


@pytest.mark.asyncio
async def test_judge_failure_persists_only_sanitized_detail_code(tmp_path: Path) -> None:
    runtime, _judge, _context, log = _runtime(
        tmp_path,
        decision=ParticipationDecisionError(
            "unknown_evidence", detail="unknown_evidence_id"
        ),
    )

    result = await runtime.evaluate_participation(_opportunity())

    assert result == {"status": "judge_failed", "reason": "unknown_evidence"}
    attempts = await log.judge_attempts_since(channel=CHANNEL, chat_id=CHAT, since_ms=0)
    assert attempts[0]["outcome"] == "unknown_evidence"
    assert attempts[0]["detail_code"] == "unknown_evidence_id"
    assert "foreign-message-id" not in json.dumps(attempts[0])
    disposition = await log.disposition("opp-1")
    assert disposition is not None and disposition["reason"] == "unknown_evidence"
    log.close()


@pytest.mark.asyncio
async def test_shadow_lane_decides_but_produces_nothing(tmp_path: Path) -> None:
    submission = _Submission()
    reactor = _Reactor()
    runtime, judge, _context, log = _runtime(
        tmp_path, decision=COMMENT, shadow=True, submission=submission, reactor=reactor
    )
    result = await runtime.evaluate_participation(_opportunity(lane="shadow"))
    assert result["status"] == "shadow" and result["action"] == "comment"
    assert judge.calls == 1
    assert submission.calls == 0
    assert reactor.calls == []
    assert await log.pending_delivery_reservations() == []
    state = await log.disposition("opp-1")
    assert state is not None and state["disposition"] == "shadow_comment"
    log.close()


@pytest.mark.asyncio
async def test_zero_budget_chat_does_not_spend_a_provider_call(tmp_path: Path) -> None:
    runtime, judge, context, log = _runtime(
        tmp_path,
        decision=COMMENT,
        policy_participation={
            "enabled": True,
            "maxUnsolicitedCommentsPerWindow": 0,
            "maxReactionsPerWindow": 0,
        },
    )
    result = await runtime.evaluate_participation(_opportunity())
    assert result["reason"] == "no_feasible_action"
    assert judge.calls == 0
    assert context.calls == 0
    log.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "policy_participation,decision",
    [
        (
            {"enabled": True, "allowInitiation": False, "allowContinuation": True},
            COMMENT,
        ),
        (
            {"enabled": True, "allowInitiation": True, "allowContinuation": False},
            ParticipationDecision(
                action="comment",
                intent="continue",
                reason="continuation",
                purpose="acknowledge the answer",
                contribution_type="observation",
                anchor_message_id="prov-1",
                target_message_id="m1",
            ),
        ),
    ],
)
async def test_comment_intent_rights_are_independent(
    tmp_path: Path,
    policy_participation: dict[str, object],
    decision: ParticipationDecision,
) -> None:
    runtime, judge, context, log = _runtime(
        tmp_path, decision=decision, policy_participation=policy_participation
    )
    result = await runtime.evaluate_participation(_opportunity())
    assert result == {"status": "judge_failed", "reason": "invalid_response"}
    assert judge.calls == 1
    assert context.inputs is not None
    assert "comment" in context.inputs.allowed_actions
    assert decision.intent not in context.inputs.allowed_intents
    log.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [("enabled", "false"), ("opted_in", 1)],
)
async def test_malformed_activation_bools_fail_closed(
    tmp_path: Path, field: str, value: object
) -> None:
    runtime, judge, context, log = _runtime(
        tmp_path,
        decision=COMMENT,
        snapshot_overrides={field: value},
    )

    result = await runtime.evaluate_participation(_opportunity())

    assert result == {"status": "skipped", "reason": "invalid_snapshot"}
    assert judge.calls == 0
    assert context.calls == 0
    log.close()


@pytest.mark.asyncio
async def test_continuation_requires_trusted_candidate_evidence(tmp_path: Path) -> None:
    decision = ParticipationDecision(
        action="comment",
        intent="continue",
        reason="follow up",
        purpose="acknowledge the answer",
        contribution_type="observation",
        anchor_message_id="prov-1",
        target_message_id="m1",
    )
    runtime, judge, context, log = _runtime(
        tmp_path,
        decision=decision,
        policy_participation={"enabled": True, "allowInitiation": False},
        snapshot_overrides={"continuation_candidate": False},
    )

    result = await runtime.evaluate_participation(_opportunity())

    assert result == {"status": "judge_failed", "reason": "invalid_response"}
    assert judge.calls == 1
    assert context.inputs is not None
    assert context.inputs.continuation_candidate is False
    assert "continue" not in context.inputs.snapshot["comment_allowed_intents"]
    log.close()


@pytest.mark.asyncio
async def test_long_unquoted_source_cannot_use_continuation_candidate_reserve(
    tmp_path: Path,
) -> None:
    """A retained anchor alone does not make unrelated long chatter continuation."""
    decision = ParticipationDecision(
        action="comment",
        intent="continue",
        reason="follow up",
        purpose="acknowledge the answer",
        contribution_type="observation",
        anchor_message_id="prov-1",
        target_message_id="m1",
    )
    runtime, judge, context, log = _runtime(
        tmp_path,
        decision=decision,
        snapshot_overrides={"continuation_candidate": True},
    )
    original_build = context.build

    async def build_with_unrelated_source(*args, **kwargs):
        rendered = await original_build(*args, **kwargs)
        rendered["channel"] = CHANNEL
        rendered["chat_id"] = CHAT
        rendered["messages"] = [
            {
                "event_id": "m1",
                "sender_id": "anna@s.whatsapp.net",
                "text": "x" * 121,
                "timestamp": NOW_MS,
                "channel": CHANNEL,
                "chat_id": CHAT,
            }
        ]
        rendered["anchors"] = [
            {
                "provider_message_id": "prov-1",
                "delivery_state": "delivered",
                "delivered_at_ms": NOW_MS - 1_000,
                "channel": CHANNEL,
                "chat_id": CHAT,
                "closed": False,
            }
        ]
        return rendered

    context.build = build_with_unrelated_source  # type: ignore[method-assign]
    result = await runtime.evaluate_participation(_opportunity())

    assert result == {"status": "judge_failed", "reason": "invalid_response"}
    assert judge.calls == 1
    assert await log.pending_delivery_reservations() == []
    attempts = await log.judge_attempts_since(
        channel=CHANNEL, chat_id=CHAT, since_ms=0
    )
    assert len(attempts) == 1
    assert attempts[0]["continuation_candidate"] == 0
    log.close()


@pytest.mark.parametrize(
    ("source", "anchor", "intervening", "expected"),
    [
        (
            {
                "event_id": "m1",
                "sender_id": "anna@s.whatsapp.net",
                "text": "x" * 121,
                "reply_to_message_id": "prov-1",
                "timestamp": NOW_MS,
            },
            {"closed": False},
            [],
            True,
        ),
        (
            {
                "event_id": "m1",
                "sender_id": "anna@s.whatsapp.net",
                "text": "short answer",
                "timestamp": NOW_MS,
            },
            {"closed": False},
            [
                {
                    "event_id": "m2",
                    "sender_id": "ben@s.whatsapp.net",
                    "text": "a foreign exchange",
                    "timestamp": NOW_MS - 500,
                }
            ],
            False,
        ),
        (
            {
                "event_id": "m1",
                "sender_id": "anna@s.whatsapp.net",
                "text": "short answer",
                "timestamp": NOW_MS,
            },
            {"closed": True},
            [],
            False,
        ),
    ],
)
def test_continuation_candidate_requires_current_social_relation(
    source: dict[str, object],
    anchor: dict[str, object],
    intervening: list[dict[str, object]],
    expected: bool,
) -> None:
    from yeoman_gateway.processing.participation_runtime import _is_continuation_candidate

    context = {
        "channel": CHANNEL,
        "chat_id": CHAT,
        "messages": [*intervening, {"channel": CHANNEL, "chat_id": CHAT, **source}],
        "anchors": [
            {
                "provider_message_id": "prov-1",
                "delivery_state": "delivered",
                "delivered_at_ms": NOW_MS - 1_000,
                "channel": CHANNEL,
                "chat_id": CHAT,
                **anchor,
            }
        ],
    }
    assert _is_continuation_candidate(_opportunity("m1"), context) is expected


@pytest.mark.asyncio
async def test_closes_exchange_survives_runtime_restart(tmp_path: Path) -> None:
    decision = ParticipationDecision(
        action="comment",
        intent="continue",
        reason="close one social exchange",
        purpose="acknowledge",
        contribution_type="observation",
        anchor_message_id="prov-1",
        target_message_id="m1",
        closes_exchange=True,
    )
    runtime, _judge, _context, log = _runtime(
        tmp_path,
        decision=decision,
        policy_participation={
            "enabled": True,
            "allowInitiation": False,
            "allowContinuation": True,
        },
        snapshot_overrides={
            "continuation_candidate": True,
            "judge_calls_per_hour": 3,
            "continuation_reserve": 0,
            "min_gap_seconds": 0,
        },
    )
    first = await runtime.evaluate_participation(_opportunity("m1"))
    assert first["status"] == "submitted"
    assert await log.social_anchor_closed(
        channel=CHANNEL, chat_id=CHAT, anchor_message_id="prov-1"
    ) is True
    log.close()

    restarted, judge, context, reopened_log = _runtime(
        tmp_path,
        decision=decision,
        policy_participation={
            "enabled": True,
            "allowInitiation": False,
            "allowContinuation": True,
        },
        snapshot_overrides={
            "continuation_candidate": True,
            "judge_calls_per_hour": 3,
            "continuation_reserve": 0,
            "min_gap_seconds": 0,
        },
    )
    result = await restarted.evaluate_participation(
        replace(_opportunity("m1"), opportunity_id="opp-2")
    )

    assert result == {"status": "judge_failed", "reason": "invalid_response"}
    assert judge.calls == 1
    assert context.calls == 1
    assert await reopened_log.social_anchor_closed(
        channel=CHANNEL, chat_id=CHAT, anchor_message_id="prov-1"
    ) is True
    attempts = await reopened_log.judge_attempts_since(
        channel=CHANNEL, chat_id=CHAT, since_ms=0
    )
    assert len(attempts) == 2
    assert attempts[0]["continuation_candidate"] == 1
    assert attempts[1]["continuation_candidate"] == 0
    reopened_log.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("veto", "expected"),
    [("pause", "paused_last_moment"), ("revoke", "source_not_authorized")],
)
async def test_last_pause_or_revocation_race_blocks_submission(
    tmp_path: Path, veto: str, expected: str
) -> None:
    """A state change observed while binding admission prevents the hand-off."""
    race_observed = asyncio.Event()
    state: dict[str, object] = {"paused": None, "allowed": True}

    class SubmissionSpy(_Submission):
        submit_calls = 0

        async def submit(self, *, admission, effect_id, content, payload_hash):
            self.submit_calls += 1
            return await super().submit(
                admission=admission,
                effect_id=effect_id,
                content=content,
                payload_hash=payload_hash,
            )

    submission = SubmissionSpy()
    runtime, _judge, _context, log = _runtime(
        tmp_path, decision=COMMENT, submission=submission
    )
    principal_calls = 0

    def principals(channel: str, chat_id: str, sources: object):
        nonlocal principal_calls
        principal_calls += 1
        if principal_calls == 2:
            state["paused"] = "paused_last_moment" if veto == "pause" else None
            state["allowed"] = veto != "revoke"
            race_observed.set()
        return tuple((str(source), "anna@s.whatsapp.net") for source in sources)

    runtime._source_principals = principals  # type: ignore[assignment]
    runtime._is_paused = lambda channel, chat_id: state["paused"]  # type: ignore[return-value]
    runtime._is_source_allowed = lambda channel, chat_id, sources: bool(
        state["allowed"]
    )

    result = await runtime.evaluate_participation(_opportunity("m1"))

    assert race_observed.is_set()
    assert result == {"status": "comment_skipped", "reason": expected}
    assert submission.calls == 1
    assert submission.submit_calls == 0
    assert await log.pending_delivery_reservations() == []
    log.close()


@pytest.mark.asyncio
async def test_changed_context_rejudges_and_reuses_unchanged_draft(tmp_path: Path) -> None:
    """One fresh same-signature admission may reuse a draft after one rejudge."""
    draft_started = asyncio.Event()
    release_draft = asyncio.Event()
    revision = {"value": 3}

    class SequenceJudge:
        calls = 0

        async def decide(self, opportunity, context):
            del opportunity, context
            self.calls += 1
            return COMMENT

    class WaitingSubmission(_Submission):
        async def generate_draft(self, *, opportunity, decision, context):
            draft_started.set()
            revision["value"] = 4
            await release_draft.wait()
            return await super().generate_draft(
                opportunity=opportunity, decision=decision, context=context
            )

    submission = WaitingSubmission()
    runtime, _judge, _context, log = _runtime(
        tmp_path,
        decision=COMMENT,
        submission=submission,
        snapshot_overrides={"max_reevaluations": 1},
    )
    base_snapshot = runtime._snapshot_provider

    def snapshot(*args, **kwargs):
        current = dict(base_snapshot(*args, **kwargs))
        current["context_revision"] = revision["value"]
        current["max_reevaluations"] = 1
        return current

    judge = SequenceJudge()
    runtime._snapshot_provider = snapshot
    runtime._judge = judge  # type: ignore[assignment]
    task = asyncio.create_task(runtime.evaluate_participation(_opportunity()))
    try:
        await asyncio.wait_for(draft_started.wait(), timeout=1)
        release_draft.set()
        result = await asyncio.wait_for(task, timeout=2)
    finally:
        release_draft.set()
        if not task.done():
            await task
    assert result["status"] == "submitted"
    assert judge.calls == 2
    assert submission.calls == 1
    log.close()


@pytest.mark.asyncio
async def test_changed_context_with_zero_reevaluations_discards_draft(tmp_path: Path) -> None:
    draft_started = asyncio.Event()
    release_draft = asyncio.Event()
    revision = {"value": 3}

    class WaitingSubmission(_Submission):
        async def generate_draft(self, *, opportunity, decision, context):
            draft_started.set()
            revision["value"] = 4
            await release_draft.wait()
            return await super().generate_draft(
                opportunity=opportunity, decision=decision, context=context
            )

    submission = WaitingSubmission()
    runtime, judge, _context, log = _runtime(
        tmp_path,
        decision=COMMENT,
        submission=submission,
        snapshot_overrides={"max_reevaluations": 0},
    )
    base_snapshot = runtime._snapshot_provider

    def snapshot(*args, **kwargs):
        current = dict(base_snapshot(*args, **kwargs))
        current["context_revision"] = revision["value"]
        current["max_reevaluations"] = 0
        return current

    runtime._snapshot_provider = snapshot
    task = asyncio.create_task(runtime.evaluate_participation(_opportunity()))
    try:
        await asyncio.wait_for(draft_started.wait(), timeout=1)
        release_draft.set()
        result = await asyncio.wait_for(task, timeout=2)
    finally:
        release_draft.set()
        if not task.done():
            await task
    assert result == {"status": "comment_skipped", "reason": "context_changed"}
    assert judge.calls == 1
    assert submission.calls == 1
    assert await log.pending_delivery_reservations() == []
    attempts = await log.judge_attempts_since(
        channel=CHANNEL, chat_id=CHAT, since_ms=0
    )
    assert len(attempts) == 1
    log.close()


@pytest.mark.asyncio
async def test_changed_decision_replaces_draft_at_most_once(tmp_path: Path) -> None:
    draft_started = asyncio.Event()
    release_draft = asyncio.Event()
    revision = {"value": 3}
    first = COMMENT
    second = ParticipationDecision(
        action="comment",
        intent="initiate",
        reason="new reason",
        purpose="a changed purpose",
        contribution_type="observation",
        target_message_id="m1",
    )

    class SequenceJudge:
        calls = 0

        async def decide(self, opportunity, context):
            del opportunity, context
            self.calls += 1
            return first if self.calls == 1 else second

    class ReplacementSubmission(_Submission):
        async def generate_draft(self, *, opportunity, decision, context):
            self.calls += 1
            if self.calls == 1:
                draft_started.set()
                revision["value"] = 4
                await release_draft.wait()
                return "old draft"
            return "replacement draft"

    submission = ReplacementSubmission()
    runtime, _judge, _context, log = _runtime(
        tmp_path,
        decision=first,
        submission=submission,
        snapshot_overrides={"max_reevaluations": 1},
    )
    base_snapshot = runtime._snapshot_provider

    def snapshot(*args, **kwargs):
        current = dict(base_snapshot(*args, **kwargs))
        current["context_revision"] = revision["value"]
        current["max_reevaluations"] = 1
        return current

    judge = SequenceJudge()
    runtime._snapshot_provider = snapshot
    runtime._judge = judge  # type: ignore[assignment]
    task = asyncio.create_task(runtime.evaluate_participation(_opportunity()))
    try:
        await asyncio.wait_for(draft_started.wait(), timeout=1)
        release_draft.set()
        result = await asyncio.wait_for(task, timeout=2)
    finally:
        release_draft.set()
        if not task.done():
            await task
    assert result["status"] == "submitted"
    assert judge.calls == 2
    assert submission.calls == 2
    assert result["effect_id"]
    assert await log.consumed_slots(
        channel=CHANNEL, chat_id=CHAT, category="comment", now_ms=NOW_MS, window_ms=1_800_000
    ) == 1
    log.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("initial_intent", "reevaluated_intent"),
    [("continue", "initiate"), ("initiate", "continue")],
)
async def test_reevaluation_cannot_change_reserved_intent(
    tmp_path: Path, initial_intent: str, reevaluated_intent: str
) -> None:
    revision = {"value": 3}
    first = replace(
        COMMENT,
        intent=initial_intent,
        anchor_message_id="prov-1" if initial_intent == "continue" else None,
    )
    second = replace(
        first,
        intent=reevaluated_intent,
        purpose="changed intent",
        anchor_message_id="prov-1" if reevaluated_intent == "continue" else None,
    )

    class SequenceJudge:
        calls = 0

        async def decide(self, opportunity, context):
            del opportunity, context
            self.calls += 1
            return first if self.calls == 1 else second

    class StaleSubmission(_Submission):
        submit_calls = 0

        async def generate_draft(self, *, opportunity, decision, context):
            revision["value"] = 4
            return await super().generate_draft(
                opportunity=opportunity, decision=decision, context=context
            )

        async def submit(self, *, admission, effect_id, content, payload_hash):
            self.submit_calls += 1
            return await super().submit(
                admission=admission,
                effect_id=effect_id,
                content=content,
                payload_hash=payload_hash,
            )

    submission = StaleSubmission()
    runtime, _judge, _context, log = _runtime(
        tmp_path,
        decision=first,
        submission=submission,
        snapshot_overrides={"max_reevaluations": 1},
    )
    base_snapshot = runtime._snapshot_provider

    def snapshot(*args, **kwargs):
        current = dict(base_snapshot(*args, **kwargs))
        current["context_revision"] = revision["value"]
        return current

    judge = SequenceJudge()
    runtime._snapshot_provider = snapshot
    runtime._judge = judge  # type: ignore[assignment]

    result = await runtime.evaluate_participation(_opportunity())

    assert result == {"status": "comment_skipped", "reason": "context_changed"}
    assert judge.calls == 2
    assert submission.calls == 1
    assert submission.submit_calls == 0
    assert await log.pending_delivery_reservations() == []
    log.close()


@pytest.mark.asyncio
async def test_reevaluation_bound_stays_at_original_activation_limit(tmp_path: Path) -> None:
    """A policy increase while stale work is pending cannot buy extra rejudges."""
    draft_started = asyncio.Event()
    release_draft = asyncio.Event()
    revision = {"value": 3}
    snapshot_calls = {"value": 0}

    class SequenceJudge:
        calls = 0

        async def decide(self, opportunity, context):
            del opportunity, context
            self.calls += 1
            if self.calls == 2:
                revision["value"] = 5
            return COMMENT

    class WaitingSubmission(_Submission):
        async def generate_draft(self, *, opportunity, decision, context):
            draft_started.set()
            revision["value"] = 4
            await release_draft.wait()
            return await super().generate_draft(
                opportunity=opportunity, decision=decision, context=context
            )

    submission = WaitingSubmission()
    runtime, _judge, _context, log = _runtime(
        tmp_path,
        decision=COMMENT,
        submission=submission,
        snapshot_overrides={"max_reevaluations": 1},
    )
    base_snapshot = runtime._snapshot_provider

    def snapshot(*args, **kwargs):
        snapshot_calls["value"] += 1
        current = dict(base_snapshot(*args, **kwargs))
        current["context_revision"] = revision["value"]
        # The fresh policy says two, but the admitted work was limited to one.
        current["max_reevaluations"] = 1 if snapshot_calls["value"] == 1 else 2
        return current

    judge = SequenceJudge()
    runtime._snapshot_provider = snapshot
    runtime._judge = judge  # type: ignore[assignment]
    task = asyncio.create_task(runtime.evaluate_participation(_opportunity()))
    try:
        await asyncio.wait_for(draft_started.wait(), timeout=1)
        release_draft.set()
        result = await asyncio.wait_for(task, timeout=2)
    finally:
        release_draft.set()
        if not task.done():
            await task
    assert result == {"status": "comment_skipped", "reason": "context_changed"}
    assert judge.calls == 2
    assert submission.calls == 1
    assert await log.pending_delivery_reservations() == []
    log.close()


@pytest.mark.asyncio
async def test_reevaluation_cannot_extend_the_original_opportunity_deadline(
    tmp_path: Path,
) -> None:
    draft_started = asyncio.Event()
    release_draft = asyncio.Event()
    revision = {"value": 3}
    now = {"value": NOW_MS}
    snapshot_calls = {"value": 0}

    class WaitingSubmission(_Submission):
        async def generate_draft(self, *, opportunity, decision, context):
            draft_started.set()
            revision["value"] = 4
            now["value"] = NOW_MS + 2_000
            await release_draft.wait()
            return await super().generate_draft(
                opportunity=opportunity, decision=decision, context=context
            )

    submission = WaitingSubmission()
    runtime, judge, _context, log = _runtime(
        tmp_path,
        decision=COMMENT,
        submission=submission,
        snapshot_overrides={
            "opportunity_ttl_seconds": 1,
            "max_reevaluations": 1,
        },
    )
    runtime._clock_ms = lambda: now["value"]
    base_snapshot = runtime._snapshot_provider

    def snapshot(*args, **kwargs):
        snapshot_calls["value"] += 1
        current = dict(base_snapshot(*args, **kwargs))
        current["context_revision"] = revision["value"]
        # A fresh policy must not extend an already admitted deadline.
        current["opportunity_ttl_seconds"] = (
            1 if snapshot_calls["value"] == 1 else 1_000
        )
        current["max_reevaluations"] = 1
        return current

    runtime._snapshot_provider = snapshot
    task = asyncio.create_task(runtime.evaluate_participation(_opportunity()))
    try:
        await asyncio.wait_for(draft_started.wait(), timeout=1)
        release_draft.set()
        result = await asyncio.wait_for(task, timeout=2)
    finally:
        release_draft.set()
        if not task.done():
            await task
    assert result == {"status": "comment_skipped", "reason": "deadline_expired"}
    assert judge.calls == 1
    assert submission.calls == 1
    log.close()


@pytest.mark.asyncio
async def test_reevaluation_stays_within_the_original_judge_budget(tmp_path: Path) -> None:
    draft_started = asyncio.Event()
    release_draft = asyncio.Event()
    revision = {"value": 3}
    snapshot_calls = {"value": 0}

    class WaitingSubmission(_Submission):
        async def generate_draft(self, *, opportunity, decision, context):
            draft_started.set()
            revision["value"] = 4
            await release_draft.wait()
            return await super().generate_draft(
                opportunity=opportunity, decision=decision, context=context
            )

    submission = WaitingSubmission()
    runtime, judge, _context, log = _runtime(
        tmp_path,
        decision=COMMENT,
        submission=submission,
        snapshot_overrides={
            "judge_calls_per_hour": 1,
            "max_reevaluations": 1,
        },
    )
    base_snapshot = runtime._snapshot_provider

    def snapshot(*args, **kwargs):
        snapshot_calls["value"] += 1
        current = dict(base_snapshot(*args, **kwargs))
        current["context_revision"] = revision["value"]
        # A refreshed policy cannot buy a second call for this admission.
        current["judge_calls_per_hour"] = (
            1 if snapshot_calls["value"] == 1 else 2
        )
        current["max_reevaluations"] = 1
        return current

    runtime._snapshot_provider = snapshot
    task = asyncio.create_task(runtime.evaluate_participation(_opportunity()))
    try:
        await asyncio.wait_for(draft_started.wait(), timeout=1)
        release_draft.set()
        result = await asyncio.wait_for(task, timeout=2)
    finally:
        release_draft.set()
        if not task.done():
            await task
    assert result == {
        "status": "comment_skipped",
        "reason": "reevaluation_budget_exhausted",
    }
    assert judge.calls == 1
    assert submission.calls == 1
    assert await log.pending_delivery_reservations() == []
    log.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("veto", "expected"),
    [("pause", "paused_chat"), ("revoke", "source_not_authorized")],
)
async def test_fresh_pause_or_revocation_discards_waiting_draft(
    tmp_path: Path, veto: str, expected: str
) -> None:
    """A post-generation policy veto releases the draft before any rejudge/send."""
    draft_started = asyncio.Event()
    release_draft = asyncio.Event()
    state: dict[str, object] = {"revision": 3, "paused": None, "allowed": True}

    class WaitingSubmission(_Submission):
        async def generate_draft(self, *, opportunity, decision, context):
            draft_started.set()
            state["revision"] = 4
            state["paused"] = "paused_chat" if veto == "pause" else None
            state["allowed"] = veto != "revoke"
            await release_draft.wait()
            return await super().generate_draft(
                opportunity=opportunity, decision=decision, context=context
            )

    submission = WaitingSubmission()
    runtime, judge, _context, log = _runtime(
        tmp_path,
        decision=COMMENT,
        submission=submission,
        snapshot_overrides={"max_reevaluations": 1},
    )
    runtime._is_paused = lambda channel, chat_id: state["paused"]  # type: ignore[return-value]
    runtime._is_source_allowed = lambda channel, chat_id, sources: bool(
        state["allowed"]
    )
    base_snapshot = runtime._snapshot_provider

    def snapshot(*args, **kwargs):
        current = dict(base_snapshot(*args, **kwargs))
        current["context_revision"] = state["revision"]
        current["max_reevaluations"] = 1
        return current

    runtime._snapshot_provider = snapshot
    task = asyncio.create_task(runtime.evaluate_participation(_opportunity()))
    try:
        await asyncio.wait_for(draft_started.wait(), timeout=1)
        release_draft.set()
        result = await asyncio.wait_for(task, timeout=2)
    finally:
        release_draft.set()
        if not task.done():
            await task
    assert result == {"status": "comment_skipped", "reason": expected}
    assert judge.calls == 1
    assert submission.calls == 1
    assert await log.pending_delivery_reservations() == []
    log.close()


@pytest.mark.asyncio
async def test_reaction_permission_is_independent_of_comment_intents(tmp_path: Path) -> None:
    runtime, judge, context, log = _runtime(
        tmp_path,
        decision=ParticipationDecision(
            action="react",
            intent="initiate",
            reason="ack",
            emoji=EMOJI,
            target_message_id="m1",
        ),
        policy_participation={
            "enabled": True,
            "allowInitiation": False,
            "allowContinuation": False,
            "allowReactions": True,
        },
    )
    result = await runtime.evaluate_participation(_opportunity())
    assert result["status"] == "reaction_submitted"
    assert judge.calls == 1
    assert context.inputs is not None
    assert context.inputs.allowed_actions == ("silence", "react")
    log.close()


@pytest.mark.asyncio
async def test_answer_caps_participation_at_react_and_comment(tmp_path: Path) -> None:
    """Removing ``react`` from the answer cap would suppress a permitted gesture."""
    runtime, _judge, context, log = _runtime(
        tmp_path, decision=REACT, reply_action="answer"
    )

    result = await runtime.evaluate_participation(_opportunity())

    assert result["status"] == "reaction_submitted"
    assert context.inputs is not None
    assert context.inputs.allowed_actions == ("silence", "react", "comment")
    log.close()


@pytest.mark.asyncio
async def test_reaction_permission_is_a_hard_veto(tmp_path: Path) -> None:
    runtime, judge, context, log = _runtime(
        tmp_path,
        decision=REACT,
        policy_participation={"enabled": True, "allowReactions": False},
    )
    result = await runtime.evaluate_participation(_opportunity())
    assert result == {"status": "judge_failed", "reason": "invalid_response"}
    assert judge.calls == 1
    assert context.inputs is not None
    assert context.inputs.allowed_actions == ("silence", "comment")
    log.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply_action,decision",
    [("silence", COMMENT), ("react", COMMENT)],
)
async def test_reply_action_caps_autonomous_effect_type(
    tmp_path: Path, reply_action: str, decision: ParticipationDecision
) -> None:
    runtime, judge, context, log = _runtime(
        tmp_path, decision=decision, reply_action=reply_action
    )
    result = await runtime.evaluate_participation(_opportunity())
    if reply_action == "silence":
        assert result == {"status": "skipped", "reason": "no_feasible_action"}
        assert judge.calls == 0
    else:
        assert result == {"status": "judge_failed", "reason": "invalid_response"}
        assert judge.calls == 1
    if reply_action == "silence":
        assert context.inputs is None
    else:
        assert context.inputs is not None
        assert context.inputs.allowed_actions == ("silence", "react")
    log.close()


@pytest.mark.asyncio
async def test_positive_but_fully_occupied_capacity_spends_no_judge_call(tmp_path: Path) -> None:
    runtime, judge, context, log = _runtime(tmp_path, decision=COMMENT)
    for index in range(3):
        await log.reserve_delivery(
            proposal_id=f"comment-holder-{index}",
            effect_id=f"comment-holder-effect-{index}",
            channel=CHANNEL,
            chat_id=CHAT,
            now_ms=NOW_MS,
            limits=(("comment", 3, 1_800_000),),
        )
    for index in range(6):
        await log.reserve_delivery(
            proposal_id=f"reaction-holder-{index}",
            effect_id=f"reaction-holder-effect-{index}",
            channel=CHANNEL,
            chat_id=CHAT,
            now_ms=NOW_MS,
            limits=(("reaction", 6, 1_800_000),),
        )
    result = await runtime.evaluate_participation(_opportunity())
    assert result == {"status": "skipped", "reason": "no_feasible_action"}
    assert judge.calls == 0
    assert context.calls == 0
    log.close()


@pytest.mark.asyncio
async def test_context_receives_actual_remaining_budgets(tmp_path: Path) -> None:
    runtime, _judge, context, log = _runtime(tmp_path, decision=SILENCE)
    await log.reserve_delivery(
        proposal_id="comment-holder",
        effect_id="comment-holder-effect",
        channel=CHANNEL,
        chat_id=CHAT,
        now_ms=NOW_MS,
        limits=(("comment", 3, 1_800_000),),
    )
    await runtime.evaluate_participation(_opportunity())
    assert context.inputs is not None
    budgets = dict(context.inputs.remaining_budgets)
    assert budgets["comments_per_window"] == 2
    assert budgets["reactions_per_window"] == 6
    log.close()


@pytest.mark.asyncio
async def test_initiation_reserves_calendar_day_and_comment_window(tmp_path: Path) -> None:
    runtime, _judge, _context, log = _runtime(tmp_path, decision=COMMENT)
    result = await runtime.evaluate_participation(_opportunity())
    assert result["status"] == "submitted"
    record = await log.delivery_record(
        proposal_id="opp-1", effect_id=str(result["effect_id"])
    )
    assert record is not None
    with log._lock:  # noqa: SLF001
        rows = [
            dict(row)
            for row in log._conn.execute(  # noqa: SLF001
                "SELECT category, window_kind FROM delivery_reservations WHERE effect_id = ?",
                (str(result["effect_id"]),),
            ).fetchall()
        ]
    assert {str(row["category"]) for row in rows} == {"comment", "initiation"}
    assert {
        str(row["category"]): str(row["window_kind"])
        for row in rows
    }["initiation"] == "calendar_day"
    log.close()


@pytest.mark.asyncio
async def test_continuation_reserves_only_comment_window(tmp_path: Path) -> None:
    decision = ParticipationDecision(
        action="comment",
        intent="continue",
        reason="follow up",
        purpose="acknowledge the answer",
        contribution_type="observation",
        anchor_message_id="prov-1",
        target_message_id="m1",
    )
    runtime, _judge, _context, log = _runtime(
        tmp_path,
        decision=decision,
        policy_participation={"enabled": True, "allowInitiation": False},
    )
    result = await runtime.evaluate_participation(_opportunity())
    assert result["status"] == "submitted"
    with log._lock:  # noqa: SLF001
        rows = log._conn.execute(  # noqa: SLF001
            "SELECT category FROM delivery_reservations WHERE effect_id = ?",
            (str(result["effect_id"]),),
        ).fetchall()
    assert [str(row["category"]) for row in rows] == ["comment"]
    log.close()


@pytest.mark.asyncio
async def test_approval_required_initiation_never_bypasses_approval_path(tmp_path: Path) -> None:
    submission = _ApprovalSubmission()
    runtime, judge, _context, log = _runtime(
        tmp_path,
        decision=COMMENT,
        submission=submission,
        approval_required=True,
    )
    result = await runtime.evaluate_participation(_opportunity())
    assert result["status"] == "awaiting_approval"
    assert result["effect_id"] == submission.approval_calls[0]["effect_id"]
    assert judge.calls == 1
    assert submission.calls == 1
    assert len(submission.approval_calls) == 1
    admission = submission.approval_calls[0]["admission"]
    assert getattr(admission, "payload_hash", "")
    rows = await log.pending_delivery_reservations(origin="participation", lane="production")
    assert {row["category"] for row in rows} == {"initiation", "comment"}
    assert {row["effect_id"] for row in rows} == {result["effect_id"]}
    assert {row["attempt_state"] for row in rows} == {"unsubmitted"}
    log.close()


@pytest.mark.asyncio
async def test_duplicate_opportunity_is_not_judged_twice(tmp_path: Path) -> None:
    runtime, judge, _context, log = _runtime(tmp_path, decision=SILENCE)
    first = await runtime.evaluate_participation(_opportunity())
    second = await runtime.evaluate_participation(_opportunity())
    assert first["status"] == "silence"
    assert second["reason"] == "attempt_not_available"
    assert judge.calls == 1
    log.close()


@pytest.mark.asyncio
async def test_snapshot_failure_skips_without_a_provider_call(tmp_path: Path) -> None:
    runtime, judge, _context, log = _runtime(tmp_path, decision=SILENCE)

    def _boom(
        channel: str, chat_id: str, *, epoch: int, opportunity: object | None = None
    ) -> dict[str, object]:
        raise RuntimeError("store unavailable")

    runtime._snapshot_provider = _boom
    result = await runtime.evaluate_participation(_opportunity())
    assert result["reason"] == "snapshot_error"
    assert judge.calls == 0
    log.close()


@pytest.mark.asyncio
async def test_comment_budget_exhaustion_skips_generation(tmp_path: Path) -> None:
    submission = _Submission()
    runtime, judge, _context, log = _runtime(
        tmp_path,
        decision=COMMENT,
        submission=submission,
        policy_participation={"enabled": True, "maxUnsolicitedCommentsPerWindow": 1},
    )
    # Another proposal already holds the single comment slot.
    await log.reserve_delivery(
        proposal_id="other",
        effect_id="other-effect",
        channel=CHANNEL,
        chat_id=CHAT,
        now_ms=NOW_MS,
        limits=(("comment", 1, 1_800_000),),
    )
    result = await runtime.evaluate_participation(_opportunity())
    assert result == {"status": "judge_failed", "reason": "invalid_response"}
    assert judge.calls == 1
    assert submission.calls == 0
    log.close()


@pytest.mark.asyncio
async def test_generation_failure_releases_the_reservation(tmp_path: Path) -> None:
    class _FailingSubmission(_Submission):
        async def generate_draft(self, *, opportunity, decision, context):
            del opportunity, decision, context
            self.calls += 1
            raise RuntimeError("provider down")

    submission = _FailingSubmission()
    runtime, _judge, _context, log = _runtime(
        tmp_path, decision=COMMENT, submission=submission
    )
    result = await runtime.evaluate_participation(_opportunity())
    assert result == {"status": "generation_failed", "reason": "generation_failed"}
    assert await log.consumed_slots(
        channel=CHANNEL, chat_id=CHAT, category="comment", now_ms=NOW_MS, window_ms=1_800_000
    ) == 0
    log.close()


@pytest.mark.asyncio
async def test_counter_vocabulary_matches_the_spec(tmp_path: Path) -> None:
    from yeoman_gateway.processing.participation_runtime import COUNTERS

    runtime, _judge, _context, log = _runtime(tmp_path, decision=SILENCE)
    await runtime.evaluate_participation(_opportunity())
    assert set(runtime.counters()) <= set(COUNTERS)
    log.close()


@pytest.mark.asyncio
async def test_context_and_judge_see_the_same_opportunity(tmp_path: Path) -> None:
    seen: list[tuple[str, tuple[str, ...]]] = []

    class _SpyJudge:
        async def decide(self, opportunity, context):
            seen.append((opportunity.opportunity_id, opportunity.source_event_ids))
            return SILENCE

    runtime, _judge, _context, log = _runtime(tmp_path, decision=SILENCE)
    runtime._judge = _SpyJudge()
    await runtime.evaluate_participation(_opportunity("m1", "m2"))
    assert seen == [("opp-1", ("m1", "m2"))]
    payload = json.dumps({"ok": True})
    assert json.loads(payload) == {"ok": True}
    log.close()


# -- scenario acceptance: orchestration and boundaries (04.4) --------------------------


def _scenarios() -> list[dict[str, object]]:
    path = Path(__file__).parent / "fixtures" / "participation_scenarios.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_scenario_dataset_is_synthetic_and_labeled() -> None:
    scenarios = _scenarios()
    assert scenarios
    for scenario in scenarios:
        assert scenario["id"]
        assert scenario["acceptable_actions"]
        for message in scenario["messages"]:  # type: ignore[union-attr]
            assert message["event_id"]
            assert "@" not in str(message["text"]) or "1555" not in str(message["text"])
        # No live identifiers: every sender is a plain synthetic name.
        for message in scenario["messages"]:  # type: ignore[union-attr]
            assert " " not in str(message["sender"])


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario_id", [item["id"] for item in _scenarios()])
async def test_scenario_reaches_judgment_and_respects_forbidden_actions(
    tmp_path: Path, scenario_id: str
) -> None:
    """Controlled judge responses prove the plumbing, not semantic quality."""
    scenario = next(item for item in _scenarios() if item["id"] == scenario_id)
    acceptable = list(scenario["acceptable_actions"])  # type: ignore[arg-type]
    action = "silence" if "silence" in acceptable else acceptable[0]
    if action == "comment":
        decision = COMMENT
    elif action == "react":
        decision = REACT
    else:
        decision = SILENCE
    runtime, judge, context, log = _runtime(tmp_path, decision=decision)
    result = await runtime.evaluate_participation(_opportunity())
    assert judge.calls == 1
    assert context.calls == 1
    assert result["status"] in {"silence", "submitted", "reaction_submitted"}
    if "comment" in set(scenario["forbidden_actions"]):  # type: ignore[arg-type]
        assert result["status"] != "submitted"
    log.close()


@pytest.mark.asyncio
async def test_unsolicited_generation_has_no_executable_tools(tmp_path: Path) -> None:
    """Hostile chat text cannot reach messaging, media, deletion, A2A or the network."""
    seen_targets: list[tuple[str, str]] = []

    class _ToolSpySubmission(_Submission):
        async def generate_draft(self, *, opportunity, decision, context):
            self.calls += 1
            seen_targets.append((opportunity.channel, opportunity.chat_id))
            assert set(context.get("allowed_actions") or ()) <= {
                "silence",
                "react",
                "comment",
            }
            return "a synthetic draft"

    submission = _ToolSpySubmission()
    runtime, _judge, _context, log = _runtime(
        tmp_path,
        decision=replace(COMMENT, target_message_id="s4-m1"),
        submission=submission,
    )
    result = await runtime.evaluate_participation(_opportunity("s4-m1"))
    assert result["status"] == "submitted"
    # The generator is invoked with the admitted target only, and the trusted
    # context carries no tool grants at all.
    assert seen_targets == [(CHANNEL, CHAT)]
    log.close()


@pytest.mark.asyncio
async def test_private_records_never_enter_the_participation_context(tmp_path: Path) -> None:
    """Owner/contact/other-channel records stay out of judge and generator input (A39)."""
    from yeoman_gateway.processing.participation_context import (
        ParticipationContextBounds,
        ParticipationContextBuilder,
        ParticipationDecisionInputs,
    )
    from yeoman_gateway.storage.inbound_archive import InboundArchive

    archive = InboundArchive(tmp_path / "inbound.db")
    archive.record_inbound(
        channel=CHANNEL,
        chat_id=CHAT,
        message_id="group-1",
        participant="anna@s.whatsapp.net",
        sender_id="anna@s.whatsapp.net",
        sender_name="anna",
        text="we meet at eight",
        timestamp=int(NOW_MS / 1000),
    )
    archive.record_inbound(
        channel=CHANNEL,
        chat_id="owner@s.whatsapp.net",
        message_id="owner-1",
        participant="owner@s.whatsapp.net",
        sender_id="owner@s.whatsapp.net",
        sender_name="owner",
        text="private owner note",
        timestamp=int(NOW_MS / 1000),
    )
    archive.record_inbound(
        channel="telegram",
        chat_id=CHAT,
        message_id="other-channel-1",
        participant="someone",
        sender_id="someone",
        sender_name="someone",
        text="same chat id, other channel",
        timestamp=int(NOW_MS / 1000),
    )
    builder = ParticipationContextBuilder(
        archive=archive,
        policy=PolicyEngine(_policy(), workspace=tmp_path),
        source_authorizer=lambda row: True,
    )
    context = await builder.build(
        _opportunity("group-1"),
        inputs=ParticipationDecisionInputs(
            snapshot={},
            bounds=ParticipationContextBounds(),
            allowed_actions=("silence",),
            allowed_intents=frozenset(("initiate",)),
            remaining_budgets=(),
            reservation_limits_by_intent=(),
            approval_required=False,
            arbitration_revision=0,
            current_source_ids=("group-1",),
            continuation_candidate=False,
        ),
        now_ms=NOW_MS,
    )
    rendered = json.dumps(context)
    assert "private owner note" not in rendered
    assert "same chat id, other channel" not in rendered
    assert "we meet at eight" in rendered


@pytest.mark.asyncio
async def test_effect_target_is_always_the_admitted_chat(tmp_path: Path) -> None:
    class _TargetSpySubmission(_Submission):
        def __init__(self) -> None:
            super().__init__()
            self.admissions: list[object] = []

        async def submit(self, *, admission, effect_id, content, payload_hash):
            self.admissions.append(admission)
            return await super().submit(
                admission=admission, effect_id=effect_id, content=content,
                payload_hash=payload_hash,
            )

    submission = _TargetSpySubmission()
    runtime, _judge, _context, log = _runtime(
        tmp_path, decision=COMMENT, submission=submission
    )
    await runtime.evaluate_participation(_opportunity())
    admission = submission.admissions[0]
    assert admission.channel == CHANNEL  # type: ignore[attr-defined]
    assert admission.chat_id == CHAT  # type: ignore[attr-defined]
    assert admission.admission_id.startswith("adm-")  # type: ignore[attr-defined]
    assert admission.source_event_ids == ("m1",)  # type: ignore[attr-defined]
    assert admission.source_principals == (("m1", "anna@s.whatsapp.net"),)  # type: ignore[attr-defined]
    assert admission.payload_hash  # type: ignore[attr-defined]
    log.close()


@pytest.mark.asyncio
async def test_comment_admission_hash_includes_reply_target(tmp_path: Path) -> None:
    class _AdmissionSpySubmission(_Submission):
        def __init__(self) -> None:
            super().__init__()
            self.admission: object | None = None

        async def submit(self, *, admission, effect_id, content, payload_hash):
            self.admission = admission
            return await super().submit(
                admission=admission,
                effect_id=effect_id,
                content=content,
                payload_hash=payload_hash,
            )

    submission = _AdmissionSpySubmission()
    runtime, _judge, _context, log = _runtime(
        tmp_path, decision=COMMENT, submission=submission
    )

    await runtime.evaluate_participation(_opportunity())

    assert submission.admission is not None
    assert submission.admission.payload_hash == payload_hash(  # type: ignore[attr-defined]
        TextPayload(text="a synthetic draft", reply_to="m1")
    )
    log.close()


@pytest.mark.asyncio
async def test_comment_without_current_target_fails_closed_at_runtime(tmp_path: Path) -> None:
    runtime, _judge, _context, log = _runtime(
        tmp_path,
        decision=replace(COMMENT, target_message_id=None),
    )

    result = await runtime.evaluate_participation(_opportunity())

    assert result == {"status": "judge_failed", "reason": "missing_target"}
    log.close()


@pytest.mark.asyncio
async def test_submission_passes_selected_target_to_writer_context() -> None:
    from yeoman_gateway.app.bootstrap import _ParticipationSubmission

    captured: dict[str, object] = {}

    class _Responder:
        async def generate_participation_draft(self, *args: object, **kwargs: object) -> str:
            del args
            captured.update(kwargs["context"])  # type: ignore[arg-type]
            return "draft"

    submission = _ParticipationSubmission(
        responder=_Responder(),
        writer_profile="participation_writer",
    )
    await submission.generate_draft(
        opportunity=_opportunity("m1", "m2"),
        decision=replace(COMMENT, target_message_id="m2"),
        context={"current_source_ids": ["m1", "m2"], "messages": []},
    )

    assert captured["target_message_id"] == "m2"


@pytest.mark.asyncio
async def test_writer_prompt_marks_only_selected_current_target() -> None:
    from yeoman_gateway.adapters.responder_llm import LLMResponder
    from yeoman_gateway.core.models import InboundEvent, PolicyDecision

    class _PromptSpy(LLMResponder):
        def __init__(self) -> None:
            self.prompt = ""

        def _metadata_for_event(self, event: InboundEvent) -> dict[str, object]:
            del event
            return {}

        async def _generate(self, **kwargs: object) -> str:
            self.prompt = str(kwargs["content"])
            return "draft"

    responder = _PromptSpy()
    await responder.generate_participation_draft(
        InboundEvent(
            channel=CHANNEL,
            chat_id=CHAT,
            sender_id="",
            content="",
            is_group=True,
        ),
        PolicyDecision(
            accept_message=False,
            should_respond=False,
            allowed_tools=frozenset(),
            reason="participation_draft_only",
        ),
        purpose="Explain the current JEV use case.",
        context={
            "current_source_ids": ["first", "selected"],
            "target_message_id": "selected",
            "messages": [
                {
                    "event_id": "first",
                    "sender": "Anna",
                    "text": "first coalesced source",
                },
                {
                    "event_id": "selected",
                    "sender": "Ben",
                    "text": "JEV needs a concrete use case",
                },
            ],
        },
        model_profile="participation_writer",
    )

    assert "[CONTEXT] Anna: first coalesced source" in responder.prompt
    assert "[CURRENT] Ben: JEV needs a concrete use case" in responder.prompt
    assert "[CURRENT] Anna" not in responder.prompt
    assert "Answer only the [CURRENT] message" in responder.prompt


def test_writer_transcript_keeps_selected_target_when_context_exceeds_limit() -> None:
    from yeoman_gateway.adapters.responder_llm import _render_participation_transcript

    transcript = _render_participation_transcript(
        {
            "target_message_id": "selected",
            "current_source_ids": ["selected"],
            "messages": [
                {"event_id": "old", "sender": "Anna", "text": "x" * 5000},
                {"event_id": "selected", "sender": "Ben", "text": "current question"},
            ],
        }
    )

    assert transcript.startswith("[CURRENT] Ben: current question")
    assert len(transcript) <= 4000


@pytest.mark.asyncio
async def test_llm_comment_replies_to_admitted_target() -> None:
    from yeoman_gateway.adapters.responder_llm import LLMResponder

    class _Sender:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def send(self, **kwargs: object) -> _Receipt:
            self.calls.append(kwargs)
            return _Receipt(effect_id=str(kwargs["effect_id"]))

    sender = _Sender()
    responder = object.__new__(LLMResponder)
    responder._service_effect_sender = sender
    admission = SimpleNamespace(
        admission_id="admission-1",
        channel=CHANNEL,
        chat_id=CHAT,
        target_message_id="m1",
    )

    result = await responder.submit_participation_comment(
        admission=admission,
        effect_id="effect-1",
        content="current answer",
    )

    assert result.status == "sent"
    assert sender.calls == [
        {
            "source": "speakup",
            "operation_ref": "participation:effect-1",
            "channel": CHANNEL,
            "chat_id": CHAT,
            "content": "current answer",
            "reply_to": "m1",
            "effect_id": "effect-1",
            "require_managed": True,
            "admission": admission,
        }
    ]


# -- final local authorization and stale work (04.3) -----------------------------------


def _authorization(**overrides: object):
    from yeoman_gateway.processing.participation_runtime import (
        ParticipationAdmission,
        ParticipationAuthorizationRequest,
    )

    admission = ParticipationAdmission(
        opportunity_id="opp-1",
        channel=CHANNEL,
        chat_id=CHAT,
        activation_epoch=1,
        lane="production",
        observed_revision=3,
        action="comment",
        intent="initiate",
    )
    base: dict[str, object] = {
        "admission": admission,
        "lane": "production",
        "is_paused": None,
        "is_shadow": False,
        "feature_enabled": True,
        "opted_in": True,
        "current_epoch": 1,
        "source_authorized": True,
        "effect_id": "e1",
        "reservation_state": "submitted",
        "payload_hash": "hash-1",
        "expected_payload_hash": "hash-1",
        "source_principals_authorized": True,
    }
    base.update(overrides)
    return ParticipationAuthorizationRequest(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "change,expected",
    [
        ({"is_paused": "paused_global"}, "paused_global"),
        ({"is_paused": "paused_chat"}, "paused_chat"),
        ({"is_shadow": True}, "shadow_lane"),
        ({"lane": "shadow"}, "shadow_lane"),
        ({"current_epoch": 2}, "epoch_changed"),
        ({"source_authorized": False}, "source_not_authorized"),
        ({"source_principals_authorized": False}, "source_principal_not_authorized"),
        ({"reservation_state": None}, "no_reservation"),
        ({"reservation_state": "reserved"}, "reservation_not_submitted"),
        ({"reservation_state": "failed"}, "reservation_failed"),
        ({"reservation_state": "cancelled"}, "reservation_cancelled"),
        ({"expected_payload_hash": ""}, "payload_hash_missing"),
        ({"payload_hash": "other"}, "payload_hash_mismatch"),
        ({"feature_enabled": False}, "feature_disabled"),
        ({"opted_in": False}, "chat_not_opted_in"),
    ],
)
def test_final_authorization_rejects_stale_or_unauthorized_effects(
    change: dict[str, object], expected: str
) -> None:
    from yeoman_gateway.processing.participation_runtime import ParticipationEffectAuthorizer

    allowed, reason = ParticipationEffectAuthorizer().check(_authorization(**change))
    assert allowed is False
    assert reason == expected


def test_final_authorization_allows_the_owned_current_effect() -> None:
    from yeoman_gateway.processing.participation_runtime import ParticipationEffectAuthorizer

    assert ParticipationEffectAuthorizer().check(_authorization()) == (True, "allow")


def test_final_authorization_rejects_admission_lane_mismatch() -> None:
    from dataclasses import replace

    from yeoman_gateway.processing.participation_runtime import ParticipationEffectAuthorizer

    request = _authorization()
    mismatched = replace(request, admission=replace(request.admission, lane="shadow"))
    assert ParticipationEffectAuthorizer().check(mismatched) == (
        False,
        "admission_lane_mismatch",
    )


@pytest.mark.asyncio
async def test_direct_request_supersedes_pending_unsolicited_work(tmp_path: Path) -> None:
    """A direct request drops speculation, and its stale effect cannot pass the gate."""
    from yeoman_gateway.consciousness.opportunities import OpportunityScheduler
    from yeoman_gateway.consciousness.participation_runtime import SourceOwner
    from yeoman_gateway.processing.participation_runtime import ParticipationEffectAuthorizer

    owner_log = SpeakupLog(tmp_path / "speakups.db")
    SourceOwner(store=owner_log)
    release = asyncio.Event()
    handled: list[str] = []

    async def handle(opportunity):
        handled.append(opportunity.opportunity_id)
        await release.wait()

    scheduler = OpportunityScheduler(handle=handle, max_concurrent_decisions=1)
    await scheduler.start()
    runtime, _judge, _context, log = _runtime(tmp_path, decision=COMMENT)
    try:
        scheduler.offer(_opportunity("m1"))
        await asyncio.sleep(0.05)
        # The activation epoch advances while the unsolicited chain is in flight.
        await log.advance_activation_epoch("participation", now_ms=NOW_MS)
        new_epoch = await log.activation_epoch("participation")
        assert new_epoch == 2
        allowed, reason = ParticipationEffectAuthorizer().check(
            _authorization(current_epoch=new_epoch)
        )
        assert (allowed, reason) == (False, "epoch_changed")
    finally:
        release.set()
        await scheduler.stop()
    # Exactly one offer was admitted before the epoch advanced; the scheduler's own
    # identity is derived from the retained sources, not from the test's constant.
    assert len(handled) == 1
    owner_log.close()
    log.close()


@pytest.mark.asyncio
async def test_social_continuation_cannot_modify_another_participants_task(
    tmp_path: Path,
) -> None:
    """A social reply has isolated lineage: it never becomes a task mutation (A09)."""
    from yeoman_gateway.processing.participation import ParticipationDecision

    decision = ParticipationDecision(
        action="comment",
        intent="continue",
        reason="react to the reply",
        purpose="acknowledge ben's answer",
        contribution_type="observation",
        anchor_message_id="prov-1",
        target_message_id="m2",
    )
    runtime, _judge, _context, log = _runtime(tmp_path, decision=decision)
    result = await runtime.evaluate_participation(_opportunity("m2"))
    assert result["status"] == "submitted"
    # The admission carries only social lineage: no task id, no thread authority.
    from yeoman_gateway.processing.participation_runtime import ParticipationAdmission

    fields = set(ParticipationAdmission.__dataclass_fields__)
    assert "task_id" not in fields
    assert "thread_id" not in fields
    assert "turn_id" not in fields
    log.close()


# -- A30: service permission never substitutes for source authorization ----------------


@pytest.mark.asyncio
async def test_denied_source_principal_blocks_participation(tmp_path: Path) -> None:
    """A denied originating participant cannot authorize a purpose (A30)."""
    from yeoman_gateway.storage.inbound_archive import InboundArchive

    archive = InboundArchive(tmp_path / "inbound.db")
    archive.record_inbound(
        channel=CHANNEL,
        chat_id=CHAT,
        message_id="m1",
        participant="mallory@s.whatsapp.net",
        sender_id="mallory@s.whatsapp.net",
        sender_name="mallory",
        text="answer me",
        timestamp=int(NOW_MS / 1000),
    )
    submitter_calls: list[str] = []

    class _SpySubmission(_Submission):
        async def generate_draft(self, *, opportunity, decision, context):
            submitter_calls.append("generated")
            return await super().generate_draft(
                opportunity=opportunity, decision=decision, context=context
            )

    runtime, judge, context, log = _runtime(
        tmp_path, decision=COMMENT, submission=_SpySubmission()
    )
    runtime._source_principals = lambda channel, chat_id, sources: tuple(
        (source_id, sender)
        for source_id, sender in archive.senders_for_messages(
            channel, chat_id, tuple(sources)
        ).items()
    )
    runtime._is_participant_allowed = lambda channel, chat_id, sender: False
    result = await runtime.evaluate_participation(_opportunity("m1"))
    assert result == {"status": "skipped", "reason": "source_principal_not_authorized"}
    assert judge.calls == 0
    assert submitter_calls == []
    log.close()


@pytest.mark.asyncio
async def test_allowed_source_principal_still_proceeds(tmp_path: Path) -> None:
    runtime, judge, _context, log = _runtime(tmp_path, decision=COMMENT)
    runtime._source_principals = lambda channel, chat_id, sources: tuple(
        (str(source), "anna@s.whatsapp.net") for source in sources
    )
    runtime._is_participant_allowed = lambda channel, chat_id, sender: sender.endswith(
        "anna@s.whatsapp.net"
    )
    result = await runtime.evaluate_participation(_opportunity("m1"))
    assert result["status"] == "submitted"
    assert judge.calls == 1
    log.close()


def test_service_permission_is_not_a_source_authorization() -> None:
    """The final authorizer refuses when only the service principal is permitted."""
    allowed, reason = _auth_check(source_principals_authorized=False)
    assert (allowed, reason) == (False, "source_principal_not_authorized")


def _auth_check(**overrides: object):
    from yeoman_gateway.processing.participation_runtime import ParticipationEffectAuthorizer

    return ParticipationEffectAuthorizer().check(_authorization(**overrides))
