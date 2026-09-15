"""One decision, then exactly the work it requires (04.2 call-count acceptance).

Real temporary ledger, real snapshot resolution from policy, controlled judge and
generator. The point of these tests is the call-count table and the boundaries, not
model quality.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from yeoman_gateway.consciousness.log import SpeakupLog, deterministic_effect_id
from yeoman_gateway.policy.engine import PolicyEngine
from yeoman_gateway.policy.schema import PolicyConfig
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

    async def build(self, opportunity):
        del opportunity
        self.calls += 1
        return {"messages": [], "anchors": [], "allowed_actions": ["silence"]}


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
                        }
                    }
                }
            }
        }
    )


def _processing(*, enabled: bool = True, shadow: bool = False) -> ProcessingConfig:
    return ProcessingConfig.model_validate(
        {
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
):
    log = SpeakupLog(tmp_path / "speakups.db")
    engine = PolicyEngine(
        _policy(opted_in=opted_in, participation=policy_participation), workspace=tmp_path
    )
    config = _processing(enabled=enabled, shadow=shadow)

    def _snapshot(channel: str, chat_id: str, *, epoch: int) -> dict[str, object]:
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
            "reaction_limits": (("reaction", reaction_limit, window_ms),)
            if reaction_limit > 0
            else (),
            "comment_limits": (("comment", comment_limit, window_ms),)
            if comment_limit > 0
            else (),
            "payload_hash": "hash-1",
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
        submission=submission if submission is not None else _Submission(),
        reactor=reactor if reactor is not None else _Reactor(),
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
    assert record is not None and record["delivery_state"] == "submitted"
    assert await log.consumed_slots(
        channel=CHANNEL, chat_id=CHAT, category="comment", now_ms=NOW_MS, window_ms=1_800_000
    ) == 1
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
    runtime._snapshot_provider = _zero_action_snapshot(runtime._snapshot_provider)
    result = await runtime.evaluate_participation(_opportunity())
    assert result["reason"] == "no_feasible_action"
    assert judge.calls == 0
    assert context.calls == 0
    log.close()


def _zero_action_snapshot(inner):
    def _snapshot(channel: str, chat_id: str, *, epoch: int) -> dict[str, object]:
        snapshot = dict(inner(channel, chat_id, epoch=epoch))
        snapshot["allowed_actions"] = ["silence"]
        return snapshot

    return _snapshot


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

    def _boom(channel: str, chat_id: str, *, epoch: int) -> dict[str, object]:
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
    assert result["reason"] == "comment_budget_exhausted"
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
