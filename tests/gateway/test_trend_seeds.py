"""Trend cards as owner-approved Participation material for quiet groups.

Synthetic chats, cards and participants only. Nothing here talks to a provider.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from yeoman_gateway.app.bootstrap import _seed_trend_cards
from yeoman_gateway.consciousness.log import SpeakupLog
from yeoman_gateway.policy.engine import PolicyEngine
from yeoman_gateway.policy.schema import PolicyConfig
from yeoman_gateway.processing.participation import (
    ParticipationDecisionError,
    ParticipationJudge,
    ParticipationOpportunity,
)
from yeoman_gateway.processing.participation_context import ParticipationContextBuilder
from yeoman_gateway.processing.participation_runtime import ParticipationRuntime
from yeoman_gateway.trends.fit import TopicFitter, parse_fits, topics_key
from yeoman_gateway.trends.models import TrendItem, TrendVerdict
from yeoman_gateway.trends.seeds import (
    MIN_SEED_GAP_MS,
    TREND_SENDER,
    TrendSeeds,
    is_trend_source,
    post_text,
    rank_cards,
)
from yeoman_gateway.trends.store import TrendStore, card_id_for

from tests.gateway.test_participation_context import (
    CHANNEL,
    CHAT,
    NOW_MS,
    OTHER_CHAT,
    _build_context,
    _record,
)
from tests.gateway.test_participation_context import _archive as _history

HOUR_MS = 3600 * 1000


def _engine(tmp_path: Path, *, trends: dict | None) -> PolicyEngine:
    chat: dict[str, Any] = {
        "participation": {"enabled": True},
        "spontaneity": {"enabled": True, "profile": "balanced"},
    }
    if trends is not None:
        chat["trends"] = trends
    return PolicyEngine(
        PolicyConfig.model_validate({"channels": {CHANNEL: {"chats": {CHAT: chat}}}}),
        workspace=tmp_path,
    )


def _store(tmp_path: Path, *cards: tuple[str, int, list[str]]) -> TrendStore:
    store = TrendStore(tmp_path / "trends.db")
    items = [TrendItem(source="google", key=title, title=title, published_at_ms=0) for title, _, _ in cards]
    verdicts = [
        TrendVerdict(
            item_key=title, score=score, kind="absurd", hook=f"about {title}",
            line=f"line on {title}", url=f"https://example.org/{len(title)}", tags=tuple(tags),
        )
        for title, score, tags in cards
    ]
    store.record(items, verdicts, min_score=7, now_ms=NOW_MS - HOUR_MS, ttl_ms=24 * HOUR_MS)
    return store


FINANCE = {"enabled": True, "topics": "Krypto, Börse", "avoid": "Fußball"}


def _fit_all(store: TrendStore, trends: dict | None, chat_id: str = CHAT) -> None:
    """Mark every stored card as fitting the chat's topics, as the fit check would."""
    if not trends or not trends.get("topics"):
        return
    ids = [card.card_id for card in store.active_cards(now_ms=NOW_MS)]
    key = topics_key(trends["topics"], trends.get("avoid", ""))
    store.record_fits(CHANNEL, chat_id, key, ids, set(ids))


def _seeds(
    tmp_path: Path, store: TrendStore, *, trends: dict | None = FINANCE, fit: bool = True
) -> TrendSeeds:
    if fit:
        _fit_all(store, trends)
    engine = _engine(tmp_path, trends=trends)
    return TrendSeeds(store=store, policy_engine=lambda: engine, clock=lambda: NOW_MS / 1000)


def test_policy_resolves_trends_block_per_chat(tmp_path: Path) -> None:
    engine = _engine(tmp_path, trends=FINANCE)
    assert engine.resolve_policy(CHANNEL, CHAT).trends.topics == "Krypto, Börse"
    assert engine.resolve_policy(CHANNEL, OTHER_CHAT).trends.enabled is False


def test_rank_drops_avoided_terms_and_puts_topic_hits_first(tmp_path: Path) -> None:
    store = _store(
        tmp_path,
        ("Mbappé Skandal", 10, ["fussball"]),
        ("Bitcoin Toaster", 7, ["krypto"]),
        ("Katze im Bundestag", 9, []),
    )
    ranked = rank_cards(
        store.unused_cards_for(CHANNEL, CHAT, now_ms=NOW_MS, limit=10),
        topics=FINANCE["topics"], avoid=FINANCE["avoid"],
    )
    assert [card.title for card in ranked] == ["Bitcoin Toaster", "Katze im Bundestag"]


def test_only_chats_with_an_enabled_block_get_cards(tmp_path: Path) -> None:
    store = _store(tmp_path, ("Bitcoin Toaster", 8, ["krypto"]))
    assert _seeds(tmp_path, store, trends=None).seed(CHANNEL, CHAT) == ()
    assert _seeds(tmp_path, store, trends={"enabled": False, "topics": "Krypto"}).seed(
        CHANNEL, CHAT
    ) == ()
    assert store.unused_cards_for(CHANNEL, CHAT, now_ms=NOW_MS)  # nothing was used up


def test_seed_uses_cards_up_binds_them_to_the_chat_and_waits_between_rounds(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path, ("Bitcoin Toaster", 8, ["krypto"]), ("Börsen-Lama", 7, []))
    seeds = _seeds(tmp_path, store)
    source_ids = seeds.seed(CHANNEL, CHAT)
    assert len(source_ids) == 2 and all(is_trend_source(item) for item in source_ids)
    assert store.unused_cards_for(CHANNEL, CHAT, now_ms=NOW_MS) == []

    row = seeds.row(CHANNEL, CHAT, source_ids[0])
    assert row is not None
    assert row["sender_id"] == TREND_SENDER
    assert row["trend_post"] == "line on Bitcoin Toaster\nhttps://example.org/15"
    assert "Themen dieser Gruppe: Krypto, Börse" in str(row["text"])
    assert "Nicht für diese Gruppe: Fußball" in str(row["text"])
    # A card offered to one chat is no source anywhere else.
    assert seeds.row(CHANNEL, OTHER_CHAT, source_ids[0]) is None
    assert seeds.sender(CHANNEL, OTHER_CHAT, source_ids[0]) is None

    _store_more = store.record(
        [TrendItem(source="google", key="Neu", title="Neu", published_at_ms=0)],
        [TrendVerdict(item_key="Neu", score=9, kind="absurd", line="neu", url="https://e.org/n")],
        min_score=7, now_ms=NOW_MS, ttl_ms=24 * HOUR_MS,
    )
    assert seeds.seed(CHANNEL, CHAT) == ()
    _fit_all(store, FINANCE)
    later = TrendSeeds(
        store=store, policy_engine=seeds._policy_engine,
        clock=lambda: (NOW_MS + MIN_SEED_GAP_MS) / 1000,
    )
    assert later.seed(CHANNEL, CHAT) == (f"trend:{card_id_for('Neu')}",)


def test_disabling_the_block_revokes_offered_cards(tmp_path: Path) -> None:
    store = _store(tmp_path, ("Bitcoin Toaster", 8, ["krypto"]))
    source_id = _seeds(tmp_path, store).seed(CHANNEL, CHAT)[0]
    off = _seeds(tmp_path, store, trends={"enabled": False})
    assert off.row(CHANNEL, CHAT, source_id) is None
    assert off.sender(CHANNEL, CHAT, source_id) is None


def test_lull_seed_registers_cards_as_new_material(tmp_path: Path) -> None:
    store = _store(tmp_path, ("Bitcoin Toaster", 8, ["krypto"]))
    seeds = _seeds(tmp_path, store)
    ledger = SpeakupLog(tmp_path / "speakups.db")
    ledger.initialize_material_baseline_sync(channel=CHANNEL, chat_id=CHAT)

    assert _seed_trend_cards(
        channel=CHANNEL, chat_id=CHAT, seeds=seeds, ledger=ledger, active=False
    ) == ()
    source_ids = _seed_trend_cards(
        channel=CHANNEL, chat_id=CHAT, seeds=seeds, ledger=ledger, active=True
    )
    material, revision = ledger.material_for_opportunity(CHANNEL, CHAT, None)
    assert material == source_ids and revision > 0


@pytest.mark.asyncio
async def test_context_shows_the_card_next_to_the_chat(tmp_path: Path) -> None:
    store = _store(tmp_path, ("Bitcoin Toaster", 8, ["krypto"]))
    seeds = _seeds(tmp_path, store)
    source_id = seeds.seed(CHANNEL, CHAT)[0]
    history = _history(tmp_path)
    builder = history.wire(
        ParticipationContextBuilder(
            policy=_engine(tmp_path, trends=FINANCE),
            source_authorizer=lambda row: True,
            trend_rows=seeds.row,
        )
    )
    _record(history, message_id="m1", text="Depot ist rot heute", minutes_ago=30)
    opportunity = ParticipationOpportunity(
        opportunity_id="opp-trend", channel=CHANNEL, chat_id=CHAT, trigger="lull",
        source_event_ids=(source_id,), observed_revision=3, activation_epoch=1,
        created_at_ms=NOW_MS,
    )
    context = await _build_context(builder, opportunity)
    messages = context["messages"]
    assert [item["message_id"] for item in messages] == ["m1", source_id]  # type: ignore[index]
    assert context["trend_posts"] == {source_id: post_text(store.active_cards(now_ms=NOW_MS)[0])}
    assert context["knowledge_selection_status"] == "disabled"

    blind = history.wire(
        ParticipationContextBuilder(
            policy=_engine(tmp_path, trends=FINANCE), source_authorizer=lambda row: True
        )
    )
    with pytest.raises(ParticipationDecisionError) as raised:
        await _build_context(blind, opportunity)
    assert raised.value.reason == "source_unavailable"


class _Client:
    route_key = "test.route"

    def __init__(self, payload: dict[str, object]) -> None:
        self._answer = json.dumps(payload)

    async def chat_with_usage(self, *args: Any, **kwargs: Any) -> Any:
        from yeoman_gateway.processing.model_route import RouteReply

        return RouteReply(content=self._answer)


def _judge_context(source_id: str) -> dict[str, object]:
    return {
        "messages": [
            {"event_id": "m1", "sender": "anna", "text": "Depot ist rot"},
            {"event_id": source_id, "sender": "Trendkarte", "text": "[Trendkarte] Bitcoin"},
        ],
        "anchors": [],
        "allowed_actions": ["silence", "react", "comment"],
        "allowed_intents": ["initiate"],
        "allowed_contribution_types": ["observation", "light_humor"],
        "current_source_ids": [source_id],
        "guidance": "",
        "direct_addressed": False,
        "allows_continuation": False,
    }


def _decision(**payload: object) -> dict[str, object]:
    base: dict[str, object] = {
        "action": "comment", "intent": "initiate", "reason": "fits",
        "evidence_ids": [], "anchor_message_id": None, "target_message_id": "trend:abc",
        "contribution_type": "light_humor", "purpose": "crypto joke for a red day",
        "emoji": None, "closes_exchange": False,
    }
    base.update(payload)
    return base


@pytest.mark.asyncio
async def test_judge_posts_a_card_only_as_an_initiated_comment() -> None:
    opportunity = ParticipationOpportunity(
        opportunity_id="opp", channel=CHANNEL, chat_id=CHAT, trigger="lull",
        source_event_ids=("trend:abc",), observed_revision=3, activation_epoch=1,
        created_at_ms=NOW_MS,
    )
    posted = await ParticipationJudge(client=_Client(_decision()), allowed_emojis=("😂",)).decide(
        opportunity, _judge_context("trend:abc")
    )
    assert (posted.action, posted.target_message_id) == ("comment", "trend:abc")

    reacted = await ParticipationJudge(
        client=_Client(_decision(action="react", emoji="😂")), allowed_emojis=("😂",)
    ).decide(opportunity, _judge_context("trend:abc"))
    assert reacted.action == "silence" and reacted.reason == "trend_card_only_posts"


@pytest.mark.asyncio
async def test_card_text_is_posted_verbatim_without_the_writer() -> None:
    class _Decision:
        target_message_id = "trend:abc"

    class _NoWriter:
        _submission = None

    text = await ParticipationRuntime._draft(
        _NoWriter(),  # type: ignore[arg-type]
        opportunity=None,  # type: ignore[arg-type]
        decision=_Decision(),  # type: ignore[arg-type]
        context={"trend_posts": {"trend:abc": "line\nhttps://e.org"}},
    )
    assert text == "line\nhttps://e.org"


@pytest.mark.asyncio
async def test_card_rules_reach_the_judge_only_with_a_card() -> None:
    seen: list[str] = []

    class _Recording(_Client):
        async def chat_with_usage(self, messages: Any, **kwargs: Any) -> Any:
            seen.append(messages[0]["content"])
            return await super().chat_with_usage(messages, **kwargs)

    opportunity = ParticipationOpportunity(
        opportunity_id="opp", channel=CHANNEL, chat_id=CHAT, trigger="lull",
        source_event_ids=("trend:abc",), observed_revision=3, activation_epoch=1,
        created_at_ms=NOW_MS,
    )
    judge = ParticipationJudge(client=_Recording(_decision()), allowed_emojis=("😂",))
    await judge.decide(opportunity, _judge_context("trend:abc"))
    plain = _judge_context("trend:abc")
    plain["messages"] = plain["messages"][:1]  # type: ignore[index]
    plain["current_source_ids"] = ["m1"]
    with pytest.raises(ParticipationDecisionError):  # the scripted card target is absent
        await judge.decide(opportunity, plain)
    assert "Trendkarte" in seen[0] and "Trendkarte" not in seen[1]


def test_with_topics_only_cards_the_fit_check_accepted_are_offered(tmp_path: Path) -> None:
    store = _store(tmp_path, ("Bitcoin Toaster", 8, ["krypto"]), ("Katze im Bundestag", 9, []))
    seeds = _seeds(tmp_path, store, fit=False)
    assert seeds.candidates(CHANNEL, CHAT) == []  # nothing judged yet
    toaster = card_id_for("Bitcoin Toaster")
    key = topics_key(FINANCE["topics"], FINANCE["avoid"])
    store.record_fits(CHANNEL, CHAT, key, [toaster, card_id_for("Katze im Bundestag")], {toaster})
    assert [card.title for card in seeds.candidates(CHANNEL, CHAT)] == ["Bitcoin Toaster"]
    # Edited topics are a new description: earlier fits do not carry over.
    edited = _seeds(tmp_path, store, trends={**FINANCE, "topics": "Autos"}, fit=False)
    assert edited.candidates(CHANNEL, CHAT) == []
    assert store.unjudged_cards_for(CHANNEL, CHAT, key, now_ms=NOW_MS) == []


@pytest.mark.asyncio
async def test_service_fits_new_cards_once_per_chat_topics(tmp_path: Path) -> None:
    from yeoman_gateway.policy.schema import TrendsPolicy
    from yeoman_gateway.trends.service import TrendsService

    store = _store(tmp_path, ("Bitcoin Toaster", 8, ["krypto"]), ("Katze im Bundestag", 9, []))
    calls: list[str] = []

    class _FitClient:
        route_key = "test.route"

        async def chat_with_usage(self, messages: Any, **kwargs: Any) -> Any:
            from yeoman_gateway.processing.model_route import RouteReply

            calls.append(messages[1]["content"])
            fits = [i for i, line in enumerate(messages[1]["content"].split("Cards:\n")[1].splitlines()) if "Bitcoin" in line]
            return RouteReply(content=json.dumps({"fits": fits}))

    class _NoSource:
        name = "none"

        async def fetch(self) -> list[TrendItem]:
            return []

    service = TrendsService(
        sources=[_NoSource()], scorer=None, store=store,  # type: ignore[arg-type]
        fitter=TopicFitter(client=_FitClient()),
        chats=lambda: [(CHANNEL, CHAT, TrendsPolicy.model_validate(FINANCE))],
        clock=lambda: NOW_MS,
    )
    report = await service.run_once()
    await service.run_once()
    assert report.fitted_chats == 1 and len(calls) == 1
    assert "Group likes: Krypto, Börse" in calls[0] and "Never: Fußball" in calls[0]
    key = topics_key(FINANCE["topics"], FINANCE["avoid"])
    assert store.fitting_card_ids(CHANNEL, CHAT, key) == {card_id_for("Bitcoin Toaster")}


def test_fit_answer_is_validated_against_the_listed_cards(tmp_path: Path) -> None:
    store = _store(tmp_path, ("Bitcoin Toaster", 8, []))
    cards = store.active_cards(now_ms=NOW_MS)
    assert parse_fits({"fits": [0, 5, -1, True, "0"]}, cards) == {cards[0].card_id}
    assert parse_fits({"fits": "all"}, cards) == set()
    assert parse_fits(None, cards) == set()


@pytest.mark.asyncio
async def test_trend_card_reaches_transport_as_a_new_unquoted_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Judge, admission, effect authorizer and transport accept an offered card."""
    from datetime import UTC, datetime

    from yeoman_gateway.adapters.policy_engine import EnginePolicyAdapter
    from yeoman_gateway.adapters.responder_llm import LLMResponder
    from yeoman_gateway.app.bootstrap import (
        _build_participation_runtime,
        build_effect_router,
        build_thread_registry,
        build_thread_responder,
    )
    from yeoman_gateway.bus.queue import MessageBus
    from yeoman_gateway.consciousness.participation_runtime import (
        ActivationEpochTracker,
        SourceOwner,
    )
    from yeoman_gateway.history.export import SecondaryArchive
    from yeoman_gateway.processing.dispatch import ServiceEffectProducer
    from yeoman_gateway.processing.store import ProcessingStore
    from yeoman_shared.config.schema import Config

    from tests.gateway.history_test_helpers import ProjectedHistory

    chat_id = "group@g.us"
    sender = "10001@s.whatsapp.net"
    store = _store(tmp_path, ("Bitcoin Toaster", 8, ["krypto"]))
    trends = {"enabled": True, "topics": "Krypto", "avoid": ""}
    _fit_all(store, trends, chat_id=chat_id)
    engine = PolicyEngine(
        PolicyConfig.model_validate({
            "defaults": {"allowedTools": {"mode": "allowlist", "tools": ["message"]}},
            "channels": {"whatsapp": {"chats": {chat_id: {
                "whoCanTalk": {"mode": "allowlist", "senders": [sender, "service:speakup"]},
                "whenToReply": {"mode": "all"},
                "spontaneity": {"enabled": True, "dailyCap": 1, "allowedActions": ["observation"]},
                "participation": {"enabled": True},
                "trends": trends,
            }}}},
        }),
        workspace=tmp_path,
    )
    seeds = TrendSeeds(store=store, policy_engine=lambda: engine, clock=lambda: NOW_MS / 1000)
    card_source = seeds.seed("whatsapp", chat_id)[0]

    class RouteClient:
        route_key = "participation.judge"

        def __init__(self, **_: object) -> None:
            pass

        async def chat_with_usage(self, *args: Any, **kwargs: Any) -> Any:
            from yeoman_gateway.processing.model_route import RouteReply

            return RouteReply(content=json.dumps({
                "action": "comment", "intent": "initiate", "reason": "quiet, fits",
                "target_message_id": card_source, "contribution_type": "observation",
                "purpose": "crypto card for a quiet group",
            }))

    class Provider:
        def get_default_model(self) -> str:
            return "unused"

        async def chat(self, *args: object, **kwargs: object) -> object:
            raise AssertionError("a trend card is posted without any writer")

    monkeypatch.setattr("yeoman_gateway.processing.model_route.RouteClient", RouteClient)
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    config = Config.model_validate({
        "models": {
            "profiles": {"participation_writer": {
                "kind": "chat", "model": "writer/model", "provider": "openrouter",
            }},
            "routes": {"participation.writer": "participation_writer"},
        },
        "processing": {
            "enabled": True,
            "chats": [f"whatsapp:{chat_id}"],
            "participation": {"enabled": True, "shadow": False, "judgeRoute": "participation.judge"},
            "reply_actions": {f"whatsapp:{chat_id}": "answer"},
        },
    })
    log = SpeakupLog(tmp_path / "speakups.db")
    archive = ProjectedHistory(tmp_path / "participation-history")
    processing = ProcessingStore(tmp_path / "processing.db")
    adapter = EnginePolicyAdapter(
        engine=engine, known_tools={"message"}, policy_path=None, workspace=tmp_path,
        processing_config=config.processing,
        activation_tracker=ActivationEpochTracker(store=log),
    )
    bus = MessageBus()
    router = build_effect_router(
        config, adapter, processing, bus, participation_ledger=log, trend_seeds=seeds
    )
    assert router is not None
    sent: list[object] = []

    async def record_text(message: object) -> dict[str, str]:
        sent.append(message)
        return {"provider_message_id": "provider-text-1"}

    async def reject_reaction(message: object) -> None:
        raise AssertionError("a card is never a reaction")

    router.set_direct_transport(record_text, reject_reaction)
    inner = LLMResponder(
        provider=Provider(),  # type: ignore[arg-type]
        workspace=tmp_path, bus=bus,
        service_effects=ServiceEffectProducer(router=router, bus=bus),
    )
    threads = build_thread_registry(config, processing)
    responder = build_thread_responder(config, processing, threads, inner, adapter, router)
    now = datetime.now(UTC)
    archive.add_message(
        channel="whatsapp", chat_id=chat_id, message_id="source-1", participant=sender,
        sender_id=sender, sender_name="person", text="ruhiger Tag heute",
        timestamp=int(now.timestamp()) - 1800,
    )
    revision = log.ensure_source_revisions_sync(
        channel="whatsapp", chat_id=chat_id, source_ids=(card_source,)
    )[0][1]
    activation = adapter.current_activation("whatsapp", chat_id)
    assert activation is not None and activation.live
    _runtime, _scheduler, decision = _build_participation_runtime(
        config=config, source_owner=SourceOwner(store=log), log=log, policy_engine=engine,
        inbound_archive=SecondaryArchive(), processing_store=processing,
        responder=responder, policy_adapter=adapter, trend_seeds=seeds,
    )
    decision_runtime, _reconciler = decision
    archive.wire(decision_runtime)
    opportunity = ParticipationOpportunity(
        opportunity_id="opportunity-trend-1", channel="whatsapp", chat_id=chat_id,
        trigger="lull", source_event_ids=(card_source,), observed_revision=revision,
        activation_epoch=activation.activation_epoch,
        created_at_ms=int(now.timestamp() * 1000),
    )
    try:
        async with archive.turn():
            result = await decision_runtime.evaluate_participation(opportunity)
        assert result.get("status") == "sent", result
        assert len(sent) == 1
        assert getattr(sent[0], "content") == "line on Bitcoin Toaster\nhttps://example.org/15"
        assert not getattr(sent[0], "reply_to", None)
    finally:
        archive.close()
        log.close()
        processing.close()
