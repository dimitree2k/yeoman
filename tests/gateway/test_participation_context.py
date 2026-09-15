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
from yeoman_gateway.processing.participation import ParticipationOpportunity
from yeoman_gateway.processing.participation_context import (
    ParticipationContextBounds,
    ParticipationContextBuilder,
)
from yeoman_gateway.processing.store import ProcessingStore
from yeoman_gateway.storage.inbound_archive import InboundArchive

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


def _archive(tmp_path: Path) -> InboundArchive:
    return InboundArchive(tmp_path / "inbound.db")


def _record(
    archive: InboundArchive,
    *,
    message_id: str,
    text: str,
    minutes_ago: float,
    sender: str = "anna",
    chat_id: str = CHAT,
    channel: str = CHANNEL,
) -> None:
    archive.record_inbound(
        channel=channel,
        chat_id=chat_id,
        message_id=message_id,
        participant=f"{sender}@s.whatsapp.net",
        sender_id=f"{sender}@s.whatsapp.net",
        sender_name=sender,
        text=text,
        timestamp=int((NOW - timedelta(minutes=minutes_ago)).timestamp()),
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


async def _builder(tmp_path: Path, *, anchors=None, taste=None):
    engine = PolicyEngine(_policy(), workspace=tmp_path)
    return ParticipationContextBuilder(
        archive=_archive(tmp_path),
        policy=engine,
        anchors=anchors,
        taste=taste,
    )


@pytest.mark.asyncio
async def test_related_emoji_five_minutes_after_a_joke_is_in_context(tmp_path: Path) -> None:
    builder = await _builder(tmp_path)
    archive = builder._archive
    _record(archive, message_id="joke", text="that goalkeeper joke again", minutes_ago=6)
    _record(archive, message_id="emoji", text="\N{THUMBS UP SIGN}", minutes_ago=5)
    context = await builder.build(_opportunity("joke", "emoji"), now_ms=NOW_MS)
    ids = {row["event_id"] for row in context["messages"]}  # type: ignore[index]
    assert {"joke", "emoji"} <= ids
    assert context["context_revision"] == 5


@pytest.mark.asyncio
async def test_intervening_participants_are_retained(tmp_path: Path) -> None:
    builder = await _builder(tmp_path)
    archive = builder._archive
    _record(archive, message_id="m1", text="who plays in goal?", minutes_ago=4, sender="anna")
    _record(archive, message_id="m2", text="not me", minutes_ago=3, sender="ben")
    _record(archive, message_id="m3", text="I could", minutes_ago=2, sender="cara")
    context = await builder.build(_opportunity("m1"), now_ms=NOW_MS)
    speakers = [row["sender"] for row in context["messages"]]  # type: ignore[index]
    assert speakers == ["anna", "ben", "cara"]


@pytest.mark.asyncio
async def test_twenty_minute_callback_is_inside_the_horizon(tmp_path: Path) -> None:
    builder = await _builder(tmp_path)
    archive = builder._archive
    _record(archive, message_id="old", text="the answer is 42", minutes_ago=20)
    context = await builder.build(_opportunity("old"), now_ms=NOW_MS)
    assert [row["event_id"] for row in context["messages"]] == ["old"]  # type: ignore[index]


@pytest.mark.asyncio
async def test_outside_the_window_is_not_included(tmp_path: Path) -> None:
    builder = await _builder(tmp_path)
    archive = builder._archive
    _record(archive, message_id="ancient", text="before the horizon", minutes_ago=600)
    context = await builder.build(_opportunity(), now_ms=NOW_MS)
    assert context["messages"] == []
    sections = [row["event_id"] for row in context["messages"]]  # type: ignore[index]
    assert "ancient" not in sections


@pytest.mark.asyncio
async def test_required_sources_survive_truncation(tmp_path: Path) -> None:
    builder = await _builder(tmp_path)
    archive = builder._archive
    _record(archive, message_id="trigger", text="the exact source", minutes_ago=1)
    for index in range(30):
        _record(
            archive,
            message_id=f"filler-{index}",
            text=f"filler {index}",
            minutes_ago=2 + index * 0.1,
        )
    context = await builder.build(
        _opportunity("trigger"),
        bounds=ParticipationContextBounds(max_messages=5, window_minutes=120),
        now_ms=NOW_MS,
    )
    ids = [row["event_id"] for row in context["messages"]]  # type: ignore[index]
    assert "trigger" in ids
    assert len(ids) == 5
    assert context["truncated_messages"] > 0


@pytest.mark.asyncio
async def test_other_chat_and_other_channel_never_leak(tmp_path: Path) -> None:
    """Hard exact-channel/chat ACL, not a prompt instruction (A39)."""
    builder = await _builder(tmp_path)
    archive = builder._archive
    _record(archive, message_id="mine", text="same chat", minutes_ago=1)
    _record(archive, message_id="theirs", text="other chat", minutes_ago=1, chat_id=OTHER_CHAT)
    _record(
        archive,
        message_id="same-id",
        text="same chat id, other channel",
        minutes_ago=1,
        channel="telegram",
    )
    context = await builder.build(_opportunity("mine"), now_ms=NOW_MS)
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
        TextPayload,
        TransportReceipt,
    )

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
    gateway.submit(
        EffectEnvelope(
            effect_id=effect_id,
            operation_key=f"test:{effect_id}",
            payload=TextPayload(text="Arvid: I can play in goal."),
            target={"channel": CHANNEL, "chat_id": CHAT},
            trace_id=effect_id,
            principal="service:speakup",
            capability="send_text",
        )
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
    builder = ParticipationContextBuilder(
        archive=_archive(tmp_path),
        policy=PolicyEngine(_policy(), workspace=tmp_path),
        anchors=DeliveryAnchorReader(log=log, store=store),
    )
    context = await builder.build(_opportunity(), now_ms=NOW_MS)
    assert context["anchors"] == []
    assert not any(
        "I can play in goal" in str(row["text"])
        for row in context["messages"]  # type: ignore[index]
    )

    await log.project_recipient_delivery(
        "p1",
        effect_id=effect_id,
        provider_message_id="prov-1",
        evidence_kind="recipient_delivery",
        evidence_ref="signal-1",
        now_ms=NOW_MS,
    )
    delivered = await builder.build(_opportunity(), now_ms=NOW_MS)
    anchors = delivered["anchors"]
    assert isinstance(anchors, list) and len(anchors) == 1
    assert anchors[0]["provider_message_id"] == "prov-1"
    assert anchors[0]["delivery_state"] == "delivered"
    log.close()
    store.close()


@pytest.mark.asyncio
async def test_context_carries_trusted_inputs_not_a_verdict(tmp_path: Path) -> None:
    builder = await _builder(tmp_path)
    context = await builder.build(_opportunity(), now_ms=NOW_MS)
    assert context["guidance"] == "Join only for football."
    assert isinstance(context["budgets"], dict)
    assert context["allowed_contribution_types"]  # existing spontaneity vocabulary
    assert "allowed_actions" in context
    assert "verdict" not in context
    assert "related" not in context


@pytest.mark.asyncio
async def test_advisory_taste_requires_provenance(tmp_path: Path) -> None:
    def _taste(channel: str, chat_id: str):
        del channel, chat_id
        return [
            {"content": "prefers short replies", "provenance": "participation:v1"},
            {"content": "old unverified pattern"},
        ]

    builder = await _builder(tmp_path, taste=_taste)
    context = await builder.build(_opportunity(), now_ms=NOW_MS)
    patterns = context["advisory_taste"]
    assert isinstance(patterns, list) and len(patterns) == 1
    assert patterns[0]["provenance"] == "participation:v1"


@pytest.mark.asyncio
async def test_media_and_forward_summaries_are_preserved(tmp_path: Path) -> None:
    builder = await _builder(tmp_path)
    archive = builder._archive
    _record(
        archive,
        message_id="img",
        text="[image_description] a whiteboard with a formation",
        minutes_ago=1,
    )
    _record(archive, message_id="q", text="does this formation work?", minutes_ago=0.5)
    context = await builder.build(_opportunity("img", "q"), now_ms=NOW_MS)
    rows = {row["event_id"]: row for row in context["messages"]}  # type: ignore[index]
    assert "whiteboard" in str(rows["img"]["media_summary"])
    assert rows["img"]["text"] == ""
    assert "formation" in str(rows["q"]["text"])
    assert context["dropped_source_ids"] == []
