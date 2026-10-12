"""Source-backed participation context: exact-chat retrieval and delivered anchors.

Synthetic chats and synthetic participants only. Nothing here talks to a provider.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from yeoman_gateway.consciousness.delivery import DeliveryAnchorReader
from yeoman_gateway.consciousness.log import SpeakupLog
from yeoman_gateway.policy.engine import PolicyEngine
from yeoman_gateway.policy.schema import PolicyConfig
from yeoman_gateway.processing.participation import (
    ParticipationDecisionError,
    ParticipationOpportunity,
)
from yeoman_gateway.processing.participation_context import (
    ParticipationContextBounds,
    ParticipationContextBuilder,
    ParticipationDecisionInputs,
)
from yeoman_gateway.processing.store import ProcessingStore

from tests.gateway.history_test_helpers import ProjectedHistory

CHANNEL = "whatsapp"
CHAT = "group@g.us"
OTHER_CHAT = "other@g.us"

#: A fixed synthetic "now" keeps the window arithmetic deterministic.
NOW = datetime(2026, 4, 25, 12, 0, tzinfo=UTC)
NOW_MS = int(NOW.timestamp() * 1000)


def _policy(*, participation: dict | None = None):
    return PolicyConfig.model_validate(
        {
            "channels": {
                CHANNEL: {
                    "chats": {
                        CHAT: {
                            "participation": participation
                            or {"enabled": True, "guidance": "Join only for football."},
                            "spontaneity": {"enabled": True, "profile": "balanced"},
                        }
                    }
                }
            }
        }
    )


def _archive(tmp_path: Path) -> ProjectedHistory:
    return ProjectedHistory(tmp_path)


def _record(
    archive: ProjectedHistory,
    *,
    message_id: str,
    text: str,
    minutes_ago: float,
    sender: str = "anna",
    chat_id: str = CHAT,
    channel: str = CHANNEL,
    reply_to_message_id: str | None = None,
) -> None:
    sender_ids = {
        "anna": "10001",
        "ben": "10002",
        "cara": "10003",
        "allowed": "10004",
        "blocked": "10005",
    }
    sender_id = f"{sender_ids.get(sender, sender)}@s.whatsapp.net"
    archive.add_message(
        channel=channel, chat_id=chat_id, message_id=message_id,
        participant=sender_id, sender_id=sender_id,
        sender_name=sender, text=text,
        timestamp=(NOW - timedelta(minutes=minutes_ago)).timestamp(),
        reply_to_message_id=reply_to_message_id,
    )


def _opportunity(*sources: str, revision: int = 5) -> ParticipationOpportunity:
    return ParticipationOpportunity(
        opportunity_id="opp-1",
        channel=CHANNEL,
        chat_id=CHAT,
        trigger="inbound",
        source_event_ids=tuple(sources),
        observed_revision=revision,
        activation_epoch=1,
        created_at_ms=NOW_MS,
    )


async def _builder(tmp_path: Path, *, anchors=None):
    engine = PolicyEngine(_policy(), workspace=tmp_path)
    history = _archive(tmp_path)
    builder = ParticipationContextBuilder(
        policy=engine,
        anchors=anchors,
        source_authorizer=lambda row: True,
    )
    return history.wire(builder)


def _context_builder(history: ProjectedHistory, tmp_path: Path, *, source_authorizer=None):
    return history.wire(ParticipationContextBuilder(
        policy=PolicyEngine(_policy(), workspace=tmp_path),
        source_authorizer=source_authorizer,
    ))


def _inputs(
    *, bounds: ParticipationContextBounds | None = None,
    current_source_ids: tuple[str, ...] = (),
    snapshot: object | None = None,
    reservation_limits_by_intent: object = (
        ("initiate", (("comment", 3, 1_800_000),)),
    ),
) -> ParticipationDecisionInputs:
    return ParticipationDecisionInputs(
        snapshot=(
            snapshot
            if snapshot is not None
            else {
                "guidance": "Join only for football.",
                "allowed_contribution_types": ("observation", "light_humor"),
                "policy_version": "test-policy",
            }
        ),
        bounds=bounds or ParticipationContextBounds(),
        allowed_actions=("silence", "react", "comment"),
        allowed_intents=frozenset(("initiate",)),
        remaining_budgets=(
            ("comments_per_window", 3),
            ("reactions_per_window", 6),
            ("judge_calls_per_hour", 12),
        ),
        reservation_limits_by_intent=reservation_limits_by_intent,  # type: ignore[arg-type]
        approval_required=False,
        arbitration_revision=1,
        current_source_ids=current_source_ids,
        continuation_candidate=False,
    )


async def _build_context(
    builder: ParticipationContextBuilder,
    opportunity: ParticipationOpportunity,
    *,
    bounds: ParticipationContextBounds | None = None,
    now_ms: int = NOW_MS,
    current_source_ids: tuple[str, ...] | None = None,
) -> dict[str, object]:
    history = builder._history_projector
    async with history.turn():
        return await builder.build(
            opportunity,
            inputs=_inputs(
                bounds=bounds,
                current_source_ids=(
                    tuple(opportunity.source_event_ids)
                    if current_source_ids is None
                    else current_source_ids
                ),
            ),
            now_ms=now_ms,
        )


@pytest.mark.asyncio
async def test_newest_required_source_is_default_reaction_target(tmp_path: Path) -> None:
    """A missing model target follows the newest current source, not list position."""
    from yeoman_gateway.processing.participation import ParticipationJudge

    builder = await _builder(tmp_path)
    archive = builder._history_projector
    _record(archive, message_id="old", text="old", minutes_ago=3)
    _record(archive, message_id="middle", text="middle", minutes_ago=2)
    _record(archive, message_id="new", text="new", minutes_ago=1)
    context = await _build_context(builder, _opportunity("new"), current_source_ids=("new",))

    class _Client:
        route_key = "test.participation"

        async def chat_with_usage(self, *args, **kwargs):
            from yeoman_gateway.processing.model_route import RouteReply
            return RouteReply(content=await self.chat(*args, **kwargs))

        async def chat(
            self,
            messages,
            *,
            max_tokens: int = 0,
            response_format: dict[str, str] | None = None,
        ) -> str:
            del messages, max_tokens, response_format
            return '{"action":"react","intent":"initiate","emoji":"👍"}'

    decision = await ParticipationJudge(client=_Client(), allowed_emojis=("👍",)).decide(
        _opportunity("new"), context
    )
    assert decision.target_message_id == "new"


@pytest.mark.asyncio
async def test_related_emoji_five_minutes_after_a_joke_is_in_context(tmp_path: Path) -> None:
    builder = await _builder(tmp_path)
    archive = builder._history_projector
    _record(archive, message_id="joke", text="that goalkeeper joke again", minutes_ago=6)
    _record(archive, message_id="emoji", text="\N{THUMBS UP SIGN}", minutes_ago=5)
    context = await _build_context(builder, _opportunity("joke", "emoji"))
    ids = {row["event_id"] for row in context["messages"]}  # type: ignore[index]
    assert {"joke", "emoji"} <= ids
    assert context["context_revision"] == 5


@pytest.mark.asyncio
async def test_intervening_participants_are_retained(tmp_path: Path) -> None:
    builder = await _builder(tmp_path)
    archive = builder._history_projector
    _record(archive, message_id="m1", text="who plays in goal?", minutes_ago=4, sender="anna")
    _record(archive, message_id="m2", text="not me", minutes_ago=3, sender="ben")
    _record(archive, message_id="m3", text="I could", minutes_ago=2, sender="cara")
    context = await _build_context(builder, _opportunity("m1"))
    speakers = [row["sender"] for row in context["messages"]]  # type: ignore[index]
    assert speakers == ["anna", "ben", "cara"]


@pytest.mark.asyncio
async def test_exact_reply_reference_reaches_participation_context(tmp_path: Path) -> None:
    builder = await _builder(tmp_path)
    archive = builder._history_projector
    _record(
        archive,
        message_id="reply",
        text="yes, exactly",
        minutes_ago=1,
        reply_to_message_id="bot-anchor-1",
    )

    context = await _build_context(builder, _opportunity("reply"))

    assert context["messages"][0]["reply_to_message_id"] == "bot-anchor-1"  # type: ignore[index]


@pytest.mark.asyncio
async def test_twenty_minute_callback_is_inside_the_horizon(tmp_path: Path) -> None:
    builder = await _builder(tmp_path)
    archive = builder._history_projector
    _record(archive, message_id="old", text="the answer is 42", minutes_ago=20)
    context = await _build_context(builder, _opportunity("old"))
    assert [row["event_id"] for row in context["messages"]] == ["old"]  # type: ignore[index]


@pytest.mark.asyncio
async def test_outside_the_window_is_not_included(tmp_path: Path) -> None:
    builder = await _builder(tmp_path)
    archive = builder._history_projector
    _record(archive, message_id="ancient", text="before the horizon", minutes_ago=600)
    context = await _build_context(builder, _opportunity())
    assert context["messages"] == []
    sections = [row["event_id"] for row in context["messages"]]  # type: ignore[index]
    assert "ancient" not in sections


@pytest.mark.asyncio
async def test_required_sources_survive_truncation(tmp_path: Path) -> None:
    builder = await _builder(tmp_path)
    archive = builder._history_projector
    _record(archive, message_id="trigger", text="the exact source", minutes_ago=1)
    for index in range(30):
        _record(
            archive,
            message_id=f"filler-{index}",
            text=f"filler {index}",
            minutes_ago=2 + index * 0.1,
        )
    context = await _build_context(
        builder,
        _opportunity("trigger"),
        bounds=ParticipationContextBounds(max_messages=5, window_minutes=120),
    )
    ids = [row["event_id"] for row in context["messages"]]  # type: ignore[index]
    assert "trigger" in ids
    assert len(ids) == 5
    assert context["truncated_messages"] > 0


@pytest.mark.asyncio
async def test_context_truncation_keeps_newest_optional_messages(tmp_path: Path) -> None:
    builder = await _builder(tmp_path)
    archive = builder._history_projector
    _record(archive, message_id="required", text="required", minutes_ago=1)
    for index in range(50):
        _record(
            archive,
            message_id=f"optional-{index}",
            text=f"optional {index}",
            minutes_ago=60 - index,
        )

    context = await _build_context(
        builder,
        _opportunity("required"),
        bounds=ParticipationContextBounds(max_messages=5, window_minutes=120),
    )
    ids = [row["event_id"] for row in context["messages"]]  # type: ignore[index]
    assert ids == ["optional-46", "optional-47", "optional-48", "optional-49", "required"]
    assert context["truncated_messages"] > 0
    assert context["dropped_source_count"] == context["truncated_messages"]


@pytest.mark.asyncio
async def test_required_sources_over_bound_fail_closed(
    tmp_path: Path,
) -> None:
    builder = await _builder(tmp_path)
    archive = builder._history_projector
    for index in range(6):
        _record(
            archive,
            message_id=f"required-{index}",
            text=f"required {index}",
            minutes_ago=6 - index,
        )

    with pytest.raises(ParticipationDecisionError, match="required_sources_exceed_message_limit"):
        await _build_context(
            builder,
            _opportunity(*(f"required-{index}" for index in range(6))),
            bounds=ParticipationContextBounds(max_messages=3, window_minutes=120),
        )


@pytest.mark.asyncio
async def test_required_source_is_exactly_looked_up_beyond_history_slice(tmp_path: Path) -> None:
    builder = await _builder(tmp_path)
    archive = builder._history_projector
    _record(archive, message_id="required", text="the trigger", minutes_ago=100)
    for index in range(50):
        _record(
            archive,
            message_id=f"filler-{index}",
            text=f"filler {index}",
            minutes_ago=99 - index,
        )

    context = await _build_context(
        builder,
        _opportunity("required"),
        bounds=ParticipationContextBounds(max_messages=5, window_minutes=120),
    )
    assert "required" in [row["event_id"] for row in context["messages"]]  # type: ignore[index]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "setup,reason",
    [
        ("missing", "source_unavailable"),
        ("expired", "source_expired"),
        ("unauthorized", "source_not_authorized"),
    ],
)
async def test_required_source_failures_are_rejected_before_judge(
    tmp_path: Path, setup: str, reason: str
) -> None:
    builder = await _builder(tmp_path)
    archive = builder._history_projector
    if setup == "expired":
        _record(archive, message_id="source", text="old", minutes_ago=600)
    elif setup == "unauthorized":
        _record(archive, message_id="source", text="blocked", minutes_ago=1)
        builder = _context_builder(archive, tmp_path, source_authorizer=lambda row: False)
    with pytest.raises(ParticipationDecisionError) as error:
        await _build_context(builder, _opportunity("source"))
    assert error.value.reason == reason


@pytest.mark.asyncio
async def test_optional_history_is_filtered_per_sender_before_rendering(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    _record(archive, message_id="required", text="keep", minutes_ago=2, sender="allowed")
    _record(archive, message_id="optional", text="do not leak", minutes_ago=1, sender="blocked")
    builder = _context_builder(archive, tmp_path, source_authorizer=lambda row: str(row.get("sender_id") or "").startswith(
            "10004@"
        ))
    context = await _build_context(builder, _opportunity("required"))
    ids = [row["event_id"] for row in context["messages"]]  # type: ignore[index]
    assert ids == ["required"]
    assert "do not leak" not in str(context)


@pytest.mark.asyncio
async def test_source_acl_requires_per_row_evidence_for_required_and_optional(
    tmp_path: Path,
) -> None:
    archive = _archive(tmp_path)
    _record(archive, message_id="required", text="required", minutes_ago=2)
    _record(archive, message_id="optional", text="optional", minutes_ago=1)
    builder = _context_builder(archive, tmp_path)

    with pytest.raises(ParticipationDecisionError) as error:
        async with archive.turn():
            await builder.build(
                _opportunity("required"),
                inputs=_inputs(current_source_ids=("required",)),
                now_ms=NOW_MS,
            )
    assert error.value.reason == "source_not_authorized"

    async with archive.turn():
        context = await builder.build(
            _opportunity("required"),
            inputs=_inputs(
                current_source_ids=("required",),
                snapshot={"guidance": "Join only for football.", "source_authorized": {"required": True}},
            ),
            now_ms=NOW_MS,
        )
    assert [row["event_id"] for row in context["messages"]] == ["required"]  # type: ignore[index]


@pytest.mark.parametrize(
    "invalid_limits",
    [
        {"initiate": (("comment", 3, 1_800_000),)},
        (("initiate", [("comment", 3, 1_800_000)]),),
    ],
)
def test_decision_inputs_reject_non_ledger_reservation_shapes(
    invalid_limits: object,
) -> None:
    with pytest.raises(TypeError, match="reservation_limits_by_intent"):
        _inputs(reservation_limits_by_intent=invalid_limits)


@pytest.mark.asyncio
async def test_other_chat_and_other_channel_never_leak(tmp_path: Path) -> None:
    """Hard exact-channel/chat ACL, not a prompt instruction (A39)."""
    builder = await _builder(tmp_path)
    archive = builder._history_projector
    _record(archive, message_id="mine", text="same chat", minutes_ago=1)
    _record(archive, message_id="theirs", text="other chat", minutes_ago=1, chat_id=OTHER_CHAT)
    _record(
        archive,
        message_id="same-id",
        text="same chat id, other channel",
        minutes_ago=1,
        channel="telegram",
    )
    context = await _build_context(builder, _opportunity("mine"))
    ids = {row["event_id"] for row in context["messages"]}  # type: ignore[index]
    assert ids == {"mine"}
    assert all(
        (row["channel"], row["chat_id"]) == (CHANNEL, CHAT)
        for row in context["messages"]  # type: ignore[index]
    )


@pytest.mark.asyncio
async def test_delivered_anchor_is_included_and_nothing_else_is(tmp_path: Path) -> None:
    from yeoman_gateway.consciousness.log import deterministic_effect_id
    from yeoman_gateway.processing.effects import EffectGateway
    from yeoman_gateway.processing.models import (
        EffectEnvelope,
        EffectReceipt,
        EffectTarget,
        TextPayload,
        TransportReceipt,
        canonical_hash,
        payload_to_mapping,
    )
    from yeoman_gateway.processing.participation_runtime import ParticipationAdmission

    class _Allow:
        def check(self, envelope, current_turn):
            del envelope, current_turn
            from yeoman_gateway.processing.models import DecisionRecord

            return DecisionRecord(
                decision_id="d1",
                trace_id="t1",
                stage="final",
                policy_version="v1",
                policy_hash="h1",
                principal="service:speakup",
                target=CHANNEL,
                capability="send_text",
                turn_revision=1,
                outcome="allow",
                reason="ok",
                created_ms=1,
            )

    class _Executor:
        async def execute(self, envelope: EffectEnvelope) -> EffectReceipt:
            return EffectReceipt(
                effect_id=envelope.effect_id,
                state="sent",
                operation_key=envelope.operation_key,
                accepted=True,
                transport_receipt=TransportReceipt(
                    channel=CHANNEL,
                    chat_id=CHAT,
                    provider_message_id="prov-1",
                    confirmed_ms=NOW_MS,
                ),
            )

    log = SpeakupLog(tmp_path / "speakups.db")
    store = ProcessingStore(tmp_path / "processing.db")
    effect_id = deterministic_effect_id(
        channel=CHANNEL, chat_id=CHAT, operation="observation", proposal_id="p1"
    )
    gateway = EffectGateway(store, authorizer=_Allow(), executor=_Executor())
    payload = TextPayload(text="Arvid: I can play in goal.")
    admission = ParticipationAdmission(
        opportunity_id="p1",
        channel=CHANNEL,
        chat_id=CHAT,
        activation_epoch=1,
        lane="production",
        observed_revision=1,
        action="comment",
        intent="initiate",
        admission_id="adm-p1",
        source_event_ids=("source-1",),
        source_principals=(("source-1", "participant@s.whatsapp.net"),),
        payload_hash=canonical_hash(payload_to_mapping(payload)),
    )
    store.enqueue_participation_effect(
        EffectEnvelope(
            effect_id=effect_id,
            operation_key=f"test:{effect_id}",
            payload=payload,
            target=EffectTarget(channel=CHANNEL, chat_id=CHAT),
            trace_id=effect_id,
            principal="service:speakup",
            capability="send_text",
            origin="participation",
            admission_id=admission.admission_id,
        ),
        admission,
    )
    await gateway.execute_ready(effect_id)
    await log.record_proposed(
        proposal_id="p1",
        channel=CHANNEL,
        chat_id=CHAT,
        action_type="observation",
        profile="balanced",
        message="Arvid: I can play in goal.",
        trigger="burst",
        context_snapshot={},
        now=1.0,
    )
    await log.reserve_delivery(
        proposal_id="p1",
        effect_id=effect_id,
        channel=CHANNEL,
        chat_id=CHAT,
        now_ms=NOW_MS,
        limits=(("comment", 5, 3_600_000),),
        origin="participation",
        lane="production",
    )
    # Accepted but not delivered: still not something Arvid said.
    await log.project_transport_accepted(
        "p1",
        effect_id=effect_id,
        provider_message_id="prov-1",
        evidence_kind="transport_receipt",
        evidence_ref="receipt-1",
        now_ms=NOW_MS,
    )
    history = _archive(tmp_path)
    builder = history.wire(ParticipationContextBuilder(
        policy=PolicyEngine(_policy(), workspace=tmp_path),
        anchors=DeliveryAnchorReader(log=log, store=store),
    ))
    context = await _build_context(builder, _opportunity())
    assert context["anchors"] == []
    assert not any(
        "I can play in goal" in str(row["text"])
        for row in context["messages"]  # type: ignore[index]
    )

    history.add_message(
        channel=CHANNEL, chat_id=CHAT, message_id="prov-1",
        sender_id="10000@s.whatsapp.net", sender_name="Arvid",
        text="Arvid: I can play in goal.", timestamp=NOW_MS / 1000, direction="out",
    )
    await log.project_recipient_delivery(
        "p1",
        effect_id=effect_id,
        provider_message_id="prov-1",
        evidence_kind="recipient_delivery",
        evidence_ref="signal-1",
        now_ms=NOW_MS,
    )
    delivered = await _build_context(builder, _opportunity())
    anchors = delivered["anchors"]
    assert isinstance(anchors, list) and len(anchors) == 1
    assert anchors[0]["provider_message_id"] == "prov-1"
    assert anchors[0]["delivery_state"] == "delivered"
    log.close()
    store.close()


@pytest.mark.asyncio
async def test_context_carries_trusted_inputs_not_a_verdict(tmp_path: Path) -> None:
    builder = await _builder(tmp_path)
    context = await _build_context(builder, _opportunity())
    assert context["guidance"] == "Join only for football."
    assert isinstance(context["budgets"], dict)
    assert context["allowed_contribution_types"]  # existing spontaneity vocabulary
    assert "allowed_actions" in context
    assert "verdict" not in context
    assert "related" not in context


@pytest.mark.asyncio
async def test_context_renders_trusted_intents_and_remaining_budgets(tmp_path: Path) -> None:
    builder = await _builder(tmp_path)
    async with builder._history_projector.turn():
        context = await builder.build(
            _opportunity(),
            inputs=ParticipationDecisionInputs(
            snapshot={
                "guidance": "Join only for football.",
                "allowed_contribution_types": ("observation",),
            },
            bounds=ParticipationContextBounds(),
            allowed_actions=("silence", "comment"),
            allowed_intents=frozenset(("continue",)),
            remaining_budgets=(("comments_per_window", 2),),
            reservation_limits_by_intent=(("continue", (("comment", 3, 1_800_000),)),),
            approval_required=False,
            arbitration_revision=4,
            current_source_ids=("required",),
            continuation_candidate=True,
            ),
            now_ms=NOW_MS,
        )
    assert context["allowed_actions"] == ["silence", "comment"]
    assert context["allowed_intents"] == ["continue"]
    assert context["remaining_budgets"] == {"comments_per_window": 2}


@pytest.mark.asyncio
async def test_media_and_forward_summaries_are_preserved(tmp_path: Path) -> None:
    builder = await _builder(tmp_path)
    archive = builder._history_projector
    _record(
        archive,
        message_id="img",
        text="[image_description] a whiteboard with a formation",
        minutes_ago=1,
    )
    _record(archive, message_id="q", text="does this formation work?", minutes_ago=0.5)
    context = await _build_context(builder, _opportunity("img", "q"))
    rows = {row["event_id"]: row for row in context["messages"]}  # type: ignore[index]
    assert "whiteboard" in str(rows["img"]["media_summary"])
    assert rows["img"]["text"] == ""
    assert "formation" in str(rows["q"]["text"])
    assert context["dropped_source_ids"] == []


@pytest.mark.asyncio
async def test_history_media_summary_keeps_authored_caption(tmp_path: Path) -> None:
    builder = await _builder(tmp_path)
    archive = builder._history_projector
    archive.add_message(chat_id=CHAT, message_id="caption", text="My formation",
        timestamp=(NOW - timedelta(minutes=1)).timestamp(), sender_id="10001@s.whatsapp.net",
        media={"kind": "image", "description": {"text": "A whiteboard"}})
    context = await _build_context(builder, _opportunity("caption"))
    row = context["messages"][0]
    assert row["text"] == "My formation"
    assert row["media_summary"] == "[Derived description]: A whiteboard"
