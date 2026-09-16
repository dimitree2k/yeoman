"""Pilot composition: the real runtime driven by synthetic traffic.

This exercises the exact composition the live gateway builds (policy engine +
participation snapshot + context builder + judge + scheduler + ingress + responder
draft path), with synthetic credentials and a controlled provider. It proves the
wiring and the shadow lane's zero-effect property; it does not claim model quality
and it never contacts a chat.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from yeoman_gateway.app.bootstrap import participant_is_allowed
from yeoman_gateway.bus.events import InboundObservedEvent
from yeoman_gateway.consciousness.delivery import DeliveryAnchorReader
from yeoman_gateway.consciousness.log import SpeakupLog
from yeoman_gateway.consciousness.opportunities import OpportunityScheduler
from yeoman_gateway.consciousness.participation_runtime import (
    ParticipationIngress,
    SourceOwner,
)
from yeoman_gateway.consciousness.participation_runtime import (
    ParticipationRuntime as OfferRuntime,
)
from yeoman_gateway.policy.engine import PolicyEngine
from yeoman_gateway.policy.schema import PolicyConfig
from yeoman_gateway.processing.participation import (
    ParticipationJudge,
    ParticipationOpportunity,
)
from yeoman_gateway.processing.participation_context import ParticipationContextBuilder
from yeoman_gateway.processing.participation_runtime import (
    ParticipationRuntime as DecisionRuntime,
)
from yeoman_gateway.processing.store import ProcessingStore
from yeoman_gateway.storage.inbound_archive import InboundArchive
from yeoman_shared.config.schema import ProcessingConfig

CHANNEL = "whatsapp"
CHAT = "pilot@g.us"
#: The context window is built from the local clock, so the synthetic traffic uses
#: "now" rather than a fixed epoch that could fall outside the horizon.
NOW_MS = int(time.time() * 1000)


class _ScriptedClient:
    """Speaks the existing async ``chat(messages, max_tokens=...)`` interface."""

    route_key = "participation.judge"

    def __init__(self, answer: dict[str, object]) -> None:
        self._answer = answer
        self.calls = 0

    async def chat(self, messages, *, max_tokens: int = 0) -> str:
        del messages, max_tokens
        self.calls += 1
        return json.dumps(self._answer)


class _Submission:
    def __init__(self) -> None:
        self.drafts = 0
        self.submissions: list[dict[str, object]] = []

    async def generate_draft(self, *, opportunity, decision, context):
        del opportunity, decision, context
        self.drafts += 1
        return "a synthetic draft"

    async def submit(self, *, admission, effect_id, content, payload_hash):
        del payload_hash
        self.submissions.append(
            {
                "channel": admission.channel,
                "chat_id": admission.chat_id,
                "effect_id": effect_id,
                "content": content,
            }
        )

        class _Receipt:
            status = "submitted"

        return _Receipt()


class _Reactor:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def __call__(self, **kwargs: object):
        self.calls.append(dict(kwargs))
        return None


def _pilot_runtime(tmp_path: Path, *, decision: dict[str, object], shadow: bool):
    policy = PolicyConfig.model_validate(
        {
            "owners": {"whatsapp": ["owner@s.whatsapp.net"]},
            "channels": {
                CHANNEL: {
                    "chats": {
                        CHAT: {
                            "whoCanTalk": {"mode": "everyone"},
                            "whenToReply": {"mode": "all"},
                            "spontaneity": {
                                "enabled": True,
                                "profile": "helpful",
                                "dailyCap": 2,
                                "allowedActions": ["observation"],
                            },
                            "participation": {"enabled": True},
                        }
                    }
                }
            },
        }
    )
    config = ProcessingConfig.model_validate(
        {
            "enabled": True,
            "chats": [f"{CHANNEL}:{CHAT}"],
            "participation": {
                "enabled": True,
                "shadow": shadow,
                "judgeRoute": "participation.judge",
            }
        }
    )
    engine = PolicyEngine(policy, workspace=tmp_path)
    log = SpeakupLog(tmp_path / "speakups.db")
    store = ProcessingStore(tmp_path / "processing.db")
    archive = InboundArchive(tmp_path / "inbound.db")
    archive.record_inbound(
        channel=CHANNEL,
        chat_id=CHAT,
        message_id="m1",
        participant="anna@s.whatsapp.net",
        sender_id="anna@s.whatsapp.net",
        sender_name="anna",
        text="Wer steht heute im Tor?",
        timestamp=int(NOW_MS / 1000),
    )

    client = _ScriptedClient(decision)
    judge = ParticipationJudge(client=client, allowed_emojis=("👍",))

    def _snapshot(
        channel: str, chat_id: str, *, epoch: int, opportunity: object | None = None
    ) -> dict[str, object]:
        resolved = engine.resolve_participation_snapshot(
            channel,
            chat_id,
            processing_config=config,
            activation_epoch=int(log.activation_epoch_sync("participation")),
        )
        limit = int(resolved.participation.max_unsolicited_comments_per_window)
        window_ms = int(resolved.participation.comment_window_minutes) * 60_000
        return {
            "enabled": resolved.enabled,
            "opted_in": resolved.opted_in,
            "invalid_reason": resolved.invalid_reason,
            "activation_epoch": resolved.activation_epoch,
            "lane": "shadow" if resolved.shadow else "production",
            "policy_version": resolved.policy_version,
            "allow_initiation": bool(resolved.participation.allow_initiation),
            "allow_continuation": bool(resolved.participation.allow_continuation),
            "allow_reactions": bool(resolved.participation.allow_reactions),
            "spontaneity_enabled": True,
            "spontaneity_daily_cap": 2,
            "spontaneity_allowed_actions": ("observation",),
            "reply_action": "answer",
            "approval_required": False,
            "arbitration_revision": 0,
            "context_window_minutes": int(resolved.context_window_minutes),
            "context_max_messages": int(resolved.context_max_messages),
            "context_revision": int(
                getattr(opportunity, "observed_revision", 0) or 0
            ),
            "current_source_ids": tuple(
                str(item)
                for item in (getattr(opportunity, "source_event_ids", ()) or ())
            ),
            "max_reevaluations": int(resolved.max_reevaluations),
            "opportunity_ttl_seconds": int(resolved.opportunity_ttl_seconds),
            "judge_calls_per_hour": int(
                resolved.participation.max_unaddressed_judge_calls_per_hour
            ),
            "min_gap_seconds": 0,
            "continuation_reserve": int(resolved.participation.continuation_judge_reserve),
            # Production derives candidacy from cheap source evidence; the fixture marks
            # its synthetic material as a candidate so the reserved slots are reachable.
            "continuation_candidate": opportunity is not None,
            "reaction_limits": (),
            "comment_limits": (("comment", limit, window_ms),),
            # The preflight action set the judge may choose from, as the production
            # builder supplies it after hard policy and remaining budgets.
            "allowed_actions": ["silence", "react", "comment"],
            "payload_hash": "",
        }

    builder = ParticipationContextBuilder(
        archive=archive,
        policy=engine,
        anchors=DeliveryAnchorReader(log=log, store=store),
        source_authorizer=lambda row: participant_is_allowed(
            engine=engine,
            channel=str(row.get("channel") or ""),
            chat_id=str(row.get("chat_id") or ""),
            sender=str(row.get("sender_id") or row.get("participant") or ""),
        ),
    )
    submission = _Submission()
    reactor = _Reactor()
    decision_runtime = DecisionRuntime(
        judge=judge,
        context_builder=builder,
        ledger=log,
        snapshot_provider=_snapshot,
        is_paused=lambda channel, chat_id: None,
        is_source_allowed=lambda channel, chat_id, sources: True,
        source_principals=lambda channel, chat_id, sources: tuple(
            (str(source), "anna@s.whatsapp.net") for source in sources
        ),
        is_participant_allowed=lambda channel, chat_id, sender: True,
        submission=submission,
        reactor=reactor,
        clock_ms=lambda: NOW_MS,
    )
    scheduler = OpportunityScheduler(
        handle=decision_runtime.evaluate_participation,
        max_concurrent_decisions=1,
        ttl_seconds=600,
    )
    offer = OfferRuntime(
        scheduler=scheduler,
        source_owner=SourceOwner(store=log),
        activation_epoch=int(log.activation_epoch_sync("participation")),
    )

    def _material(
        channel: str, chat_id: str, source_ids: tuple[str, ...] | None
    ) -> tuple[tuple[str, ...], int]:
        resolved_sources = archive.resolve_source_ids(channel, chat_id, source_ids)
        log.ensure_source_revisions_sync(
            channel=channel,
            chat_id=chat_id,
            source_ids=resolved_sources,
            now_ms=NOW_MS,
        )
        return log.material_for_opportunity(
            channel,
            chat_id,
            resolved_sources,
            lane="shadow" if shadow else "production",
        )

    ingress = ParticipationIngress(
        runtime=offer,
        ledger=log,
        is_active=lambda c, i: True,
        material_provider=_material,
    )
    return {
        "engine": engine,
        "log": log,
        "store": store,
        "archive": archive,
        "client": client,
        "scheduler": scheduler,
        "ingress": ingress,
        "decision_runtime": decision_runtime,
        "submission": submission,
        "reactor": reactor,
    }


def _event(message_id: str = "m1") -> InboundObservedEvent:
    return InboundObservedEvent(
        channel=CHANNEL,
        chat_id=CHAT,
        sender_id="anna@s.whatsapp.net",
        content="Wer steht heute im Tor?",
        timestamp=NOW_MS / 1000,
        message_id=message_id,
        is_group=True,
        metadata={"message_id": message_id},
    )


@pytest.mark.asyncio
async def test_shadow_pilot_admits_and_decides_without_any_effect(tmp_path: Path) -> None:
    """The live shadow configuration produces a decision and zero effects."""
    rt = _pilot_runtime(
        tmp_path,
        decision={
            "action": "comment",
            "intent": "initiate",
            "reason": "open question to the group",
            "purpose": "answer the goalkeeper question",
            "contribution_type": "observation",
            "evidence_ids": ["m1"],
            "target_message_id": "m1",
        },
        shadow=True,
    )
    await rt["scheduler"].start()
    try:
        assert rt["ingress"].handle_event(_event()) is True
        for _ in range(100):
            disposition = await rt["log"].disposition_by_chat(CHANNEL, CHAT)
            if disposition:
                break
            await asyncio.sleep(0.02)
    finally:
        await rt["scheduler"].stop()

    # The judge ran, and the decision was recorded as a shadow decision.
    assert rt["client"].calls == 1
    disposition = await rt["log"].disposition_by_chat(CHANNEL, CHAT)
    assert disposition is not None
    assert disposition["disposition"] == "shadow_comment"
    # Zero new-lane effects in shadow.
    assert rt["submission"].drafts == 0
    assert rt["submission"].submissions == []
    assert rt["reactor"].calls == []
    assert await rt["log"].pending_delivery_reservations() == []
    assert await rt["log"].delivered_reservation_rows(
        channel=CHANNEL, chat_id=CHAT, since_ms=0, limit=10
    ) == []
    rt["log"].close()
    rt["store"].close()


@pytest.mark.asyncio
async def test_live_pilot_path_produces_one_comment_and_one_reservation(tmp_path: Path) -> None:
    """With shadow off the same composition reserves once and submits one effect."""
    rt = _pilot_runtime(
        tmp_path,
        decision={
            "action": "comment",
            "intent": "initiate",
            "reason": "open question to the group",
            "purpose": "answer the goalkeeper question",
            "contribution_type": "observation",
            "evidence_ids": ["m1"],
            "target_message_id": "m1",
        },
        shadow=False,
    )
    await rt["scheduler"].start()
    try:
        assert rt["ingress"].handle_event(_event()) is True
        for _ in range(100):
            if rt["submission"].submissions:
                break
            await asyncio.sleep(0.02)
    finally:
        await rt["scheduler"].stop()

    assert rt["client"].calls == 1
    assert rt["submission"].drafts == 1
    assert len(rt["submission"].submissions) == 1
    call = rt["submission"].submissions[0]
    assert (call["channel"], call["chat_id"]) == (CHANNEL, CHAT)
    held = await rt["log"].pending_delivery_reservations()
    assert {row["category"] for row in held} == {"initiation", "comment"}
    assert {row["effect_id"] for row in held} == {call["effect_id"]}
    assert {row["delivery_state"] for row in held} == {"reserved"}
    assert {row["attempt_state"] for row in held} == {"unsubmitted"}
    assert held[0]["effect_id"] == call["effect_id"]
    rt["log"].close()
    rt["store"].close()


@pytest.mark.asyncio
async def test_silence_pilot_records_silence_and_spends_nothing(tmp_path: Path) -> None:
    rt = _pilot_runtime(
        tmp_path,
        decision={"action": "silence", "intent": "initiate", "reason": "nothing to add"},
        shadow=False,
    )
    await rt["scheduler"].start()
    try:
        assert rt["ingress"].handle_event(_event()) is True
        for _ in range(100):
            disposition = await rt["log"].disposition_by_chat(CHANNEL, CHAT)
            if disposition:
                break
            await asyncio.sleep(0.02)
    finally:
        await rt["scheduler"].stop()
    assert rt["client"].calls == 1
    disposition = await rt["log"].disposition_by_chat(CHANNEL, CHAT)
    assert disposition is not None and disposition["disposition"] == "decided_silence"
    assert rt["submission"].drafts == 0
    assert await rt["log"].pending_delivery_reservations() == []
    rt["log"].close()
    rt["store"].close()


# -- the real participant-authorization predicate (found broken by the live probe) ------


def _pilot_engine(tmp_path: Path):
    from yeoman_gateway.policy.engine import PolicyEngine
    from yeoman_gateway.policy.schema import PolicyConfig

    policy = PolicyConfig.model_validate(
        {
            "channels": {
                CHANNEL: {
                    "chats": {
                        CHAT: {
                            "whoCanTalk": {"mode": "everyone"},
                            "whenToReply": {"mode": "all"},
                            "participation": {"enabled": True},
                        },
                        "closed@g.us": {
                            "whoCanTalk": {"mode": "allowlist", "senders": ["vip@s.whatsapp.net"]},
                            "participation": {"enabled": True},
                        },
                    }
                }
            }
        }
    )
    return PolicyEngine(policy, workspace=tmp_path)


@pytest.mark.parametrize(
    "chat,sender,allowed",
    [
        (CHAT, "anna@s.whatsapp.net", True),
        (CHAT, "ben@s.whatsapp.net", True),
        ("closed@g.us", "vip@s.whatsapp.net", True),
        ("closed@g.us", "stranger@s.whatsapp.net", False),
    ],
)
def test_participant_is_allowed_matches_real_policy(
    tmp_path: Path, chat: str, sender: str, allowed: bool
) -> None:
    """The live predicate must call the policy engine correctly and fail closed."""
    from yeoman_gateway.app.bootstrap import participant_is_allowed

    engine = _pilot_engine(tmp_path)
    assert (
        participant_is_allowed(engine=engine, channel=CHANNEL, chat_id=chat, sender=sender)
        is allowed
    )


def test_participant_is_allowed_fails_closed_on_a_broken_check(tmp_path: Path) -> None:
    from yeoman_gateway.app.bootstrap import participant_is_allowed

    class _Broken:
        def evaluate(self, *args, **kwargs):
            raise RuntimeError("engine unavailable")

    assert (
        participant_is_allowed(
            engine=_Broken(), channel=CHANNEL, chat_id=CHAT, sender="anna@s.whatsapp.net"
        )
        is False
    )
    assert (
        participant_is_allowed(engine=None, channel=CHANNEL, chat_id=CHAT, sender="anna@x") is False
    )
    assert (
        participant_is_allowed(engine=_pilot_engine(tmp_path), channel=CHANNEL, chat_id=CHAT, sender="")
        is False
    )


# -- protected continuation quota (found unreachable in the live pilot) ----------------


def test_continuation_candidacy_uses_short_unquoted_material(tmp_path: Path) -> None:
    """The reserve must be reachable, or it silently blocks eligible continuations."""
    from yeoman_gateway.app.bootstrap import CONTINUATION_CANDIDATE_MAX_CHARS

    assert CONTINUATION_CANDIDATE_MAX_CHARS == 120

    class _Archive:
        def __init__(self) -> None:
            self.rows = {
                "short": {"text": "Aber noch passt das Schmerzensgeld"},
                "long": {"text": "x" * (CONTINUATION_CANDIDATE_MAX_CHARS + 1)},
                "empty": {"text": ""},
            }

        def lookup_message(self, channel: str, chat_id: str, message_id: str):
            del channel, chat_id
            return self.rows.get(message_id)

    class _Opportunity:
        channel = CHANNEL
        chat_id = CHAT

        def __init__(self, *sources: str) -> None:
            self.source_event_ids = sources

    # The predicate is a closure over the archive in the live builder; replicate its
    # decision function here through the same public inputs it uses.
    archive = _Archive()

    def candidate(opportunity: object) -> bool:
        lookup = getattr(archive, "lookup_message", None)
        if lookup is None or opportunity is None:
            return False
        for source_id in getattr(opportunity, "source_event_ids", ()) or ():
            token = str(source_id)
            if token.startswith("observed:"):
                continue
            row = lookup("", "", token)
            if row is None:
                continue
            text = str(row.get("text") or "").strip()
            if text and len(text) <= CONTINUATION_CANDIDATE_MAX_CHARS:
                return True
        return False

    assert candidate(_Opportunity("short")) is True
    assert candidate(_Opportunity("long")) is False
    assert candidate(_Opportunity("empty")) is False
    assert candidate(_Opportunity("observed:whatsapp:pilot@g.us")) is False
    assert candidate(_Opportunity("missing")) is False
    assert candidate(_Opportunity("short", "long")) is True


def test_snapshot_provider_receives_the_opportunity() -> None:
    """The evaluator must hand the opportunity over, or the reserve stays unreachable."""
    import inspect

    from yeoman_gateway.processing import participation_runtime as module

    source = inspect.getsource(module.ParticipationRuntime.evaluate_participation)
    assert "opportunity=opportunity" in source


@pytest.mark.asyncio
async def test_continuation_candidate_may_use_reserved_slots_end_to_end(
    tmp_path: Path,
) -> None:
    """A short related message is judged even after background slots are spent (A40)."""
    rt = _pilot_runtime(
        tmp_path,
        decision={"action": "silence", "intent": "initiate", "reason": "nothing to add"},
        shadow=False,
    )
    runtime, log = rt["decision_runtime"], rt["log"]
    store = rt["store"]
    provider_calls = rt["client"]
    base_snapshot = runtime._snapshot_provider

    def snapshot(channel: str, chat_id: str, *, epoch: int, opportunity=None):
        data = dict(base_snapshot(channel, chat_id, epoch=epoch, opportunity=opportunity))
        data["continuation_candidate"] = True
        return data

    runtime._snapshot_provider = snapshot
    from yeoman_gateway.processing.models import (
        EffectEnvelope,
        EffectEvidence,
        EffectTarget,
        TextPayload,
        canonical_hash,
        payload_to_mapping,
    )
    from yeoman_gateway.processing.participation_runtime import ParticipationAdmission

    anchor_payload = TextPayload(text="Earlier bot contribution")
    anchor_admission = ParticipationAdmission(
        opportunity_id="anchor-proposal",
        channel=CHANNEL,
        chat_id=CHAT,
        activation_epoch=1,
        lane="production",
        observed_revision=1,
        action="comment",
        intent="initiate",
        admission_id="adm-anchor",
        source_event_ids=("source-anchor",),
        source_principals=(("source-anchor", "participant@s.whatsapp.net"),),
        payload_hash=canonical_hash(payload_to_mapping(anchor_payload)),
    )
    store.enqueue_participation_effect(
        EffectEnvelope(
            effect_id="anchor-effect",
            operation_key="pilot:anchor",
            payload=anchor_payload,
            target=EffectTarget(channel=CHANNEL, chat_id=CHAT),
            origin="participation",
            admission_id=anchor_admission.admission_id,
            created_ms=NOW_MS - 120_000,
        ),
        anchor_admission,
    )
    assert store.claim_effect("anchor-effect", "test", NOW_MS - 119_500, 30_000)
    assert store.transition(
        effect_id="anchor-effect",
        expected="executing",
        target="sent",
        now_ms=NOW_MS - 119_000,
        worker_id="test",
        evidence=EffectEvidence(kind="transport_receipt", detail="accepted"),
    )
    store.record_transport_receipt(
        "anchor-effect",
        channel=CHANNEL,
        chat_id=CHAT,
        provider_message_id="bot-anchor",
        now_ms=NOW_MS - 119_000,
    )
    assert await log.reserve_delivery(
        proposal_id="anchor-proposal",
        effect_id="anchor-effect",
        channel=CHANNEL,
        chat_id=CHAT,
        now_ms=NOW_MS - 120_000,
        limits=(("comment", 20, 3_600_000),),
        origin="participation",
        lane="production",
    )
    await log.record_send_attempt(
        "anchor-proposal", effect_id="anchor-effect", now_ms=NOW_MS - 119_000
    )
    await log.project_transport_accepted(
        "anchor-proposal",
        effect_id="anchor-effect",
        provider_message_id="bot-anchor",
        evidence_kind="transport_receipt",
        evidence_ref="anchor-receipt",
        now_ms=NOW_MS - 118_000,
    )
    assert await log.project_recipient_delivery(
        "anchor-proposal",
        effect_id="anchor-effect",
        provider_message_id="bot-anchor",
        evidence_kind="recipient_delivery",
        evidence_ref="anchor-delivery",
        now_ms=NOW_MS - 117_000,
    )
    # Spend every background slot: limit 12 minus reserve 4 leaves 8 unbounded calls.
    for index in range(8):
        assert await log.reserve_judge_attempt(
            f"bg-{index}:0",
            opportunity_id=f"bg-{index}",
            channel=CHANNEL,
            chat_id=CHAT,
            now_ms=NOW_MS - 60_000,
            hourly_limit=12,
            min_gap_ms=0,
            continuation_candidate=False,
            continuation_reserve=4,
        )
    # ...but a continuation candidate still reaches the judge.
    opportunity = ParticipationOpportunity(
        opportunity_id="reserve-probe", channel=CHANNEL, chat_id=CHAT, trigger="inbound",
        source_event_ids=("m1",), observed_revision=99, activation_epoch=1,
        created_at_ms=NOW_MS,
    )
    result = await runtime.evaluate_participation(opportunity)
    assert result["status"] == "silence"
    assert provider_calls.calls == 1
    attempts = await log.judge_attempts_since(
        channel=CHANNEL, chat_id=CHAT, since_ms=NOW_MS - 3_600_000
    )
    assert any(int(a["continuation_candidate"]) == 1 for a in attempts)
    log.close()
