"""Loose follow-ups join a thread only through chat structure, never through wording."""

from __future__ import annotations

from pathlib import Path

from yeoman_gateway.processing.models import CanonicalEvent
from yeoman_gateway.processing.routing import PreviousMessage, continuity_signal
from yeoman_gateway.processing.store import ProcessingStore
from yeoman_gateway.processing.threads import JoinRule, ThreadRegistry

T0 = 1_700_000_000_000
CHAT = "chat@g.us"
OWNER = "owner@s.whatsapp.net"
ALEX = "alex@s.whatsapp.net"


class _Config:
    class Threads:
        followup_window_seconds = 600
        idle_seconds = 1800
        reopen_window_seconds = 604800
        pending_inputs_per_thread = 32

    ambient_chats: list[str] = [f"whatsapp:{CHAT}"]
    reply_actions: dict[str, str] = {}


def _event(eid: str, *, sender: str = OWNER, mentioned: bool = False, at_ms: int,
           reply_to: str | None = None, origin: str = "whatsapp") -> CanonicalEvent:
    payload: dict[str, object] = {"text": "any words", "is_group": True, "mentioned_bot": mentioned}
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    return CanonicalEvent(
        event_id=eid, event_key=f"k-{eid}-{origin}", trace_id=f"t-{eid}", kind="message",
        origin=origin, channel="whatsapp", chat_id=CHAT, principal=sender,
        source_message_id=eid.split("#")[0], occurred_ms=at_ms, payload=payload,
    )


def _say(store: ProcessingStore, registry: ThreadRegistry, event: CanonicalEvent,
         *, assign: bool = True):
    store.append_event(event_key=event.event_key, event_id=event.event_id,
                       trace_id=event.trace_id, payload=event, now_ms=event.occurred_ms)
    return registry.assign(event, now_ms=event.occurred_ms) if assign else None


def _bot(store: ProcessingStore, decision, *, at_ms: int, message_id: str) -> None:
    store.register_thread_message(thread_id=decision.thread_id, turn_id=decision.turn_id,
                                  now_ms=at_ms, direction="out", effect_id=f"fx-{message_id}",
                                  message_id=message_id)


def _setup(tmp_path: Path) -> tuple[ProcessingStore, ThreadRegistry]:
    store = ProcessingStore(tmp_path / "p.db")
    return store, ThreadRegistry(store=store, config=_Config())


def test_signal_is_decided_by_structure_only() -> None:
    def signal(previous, **kwargs):
        return continuity_signal(candidate_thread_id="th", principal=OWNER,
                                 previous=previous, **kwargs)

    own = PreviousMessage("p", False, frozenset({OWNER}), frozenset({"th"}), T0)
    assert signal(own, now_ms=T0 + 1_000, same_sender_gap_ms=120_000).kind == "adjacent_same_sender"
    assert not signal(own, now_ms=T0 + 121_000, same_sender_gap_ms=120_000).positive
    assert not signal(own, mentioned_bot=True).positive
    bot = PreviousMessage("b", True, frozenset(), frozenset({"th"}), T0)
    assert signal(bot, mentioned_bot=True).kind == "adjacent_bot_reply"
    assert not signal(PreviousMessage("p", False, frozenset({ALEX}), frozenset({"th"}))).positive
    assert not signal(PreviousMessage("p", False, frozenset({OWNER}), frozenset({"other"}))).positive
    assert not signal(None).positive


def test_incident_owner_question_after_human_chain_does_not_join_old_bot_thread(tmp_path: Path) -> None:
    store, registry = _setup(tmp_path)
    old = _say(store, registry, _event("q-old", mentioned=True, at_ms=T0))
    _bot(store, old, at_ms=T0 + 60_000, message_id="bot-old")
    _say(store, registry, _event("video", at_ms=T0 + 4 * 60_000))
    _say(store, registry, _event("alex", sender=ALEX, at_ms=T0 + 5 * 60_000, reply_to="video"))
    _say(store, registry, _event("lights", at_ms=T0 + 8 * 60_000, reply_to="alex"))

    question = _say(store, registry, _event("question", mentioned=True, at_ms=T0 + 9 * 60_000))

    assert question.thread_id != old.thread_id
    assert question.rule is not JoinRule.FOLLOWUP_SINGLE_ACTIVE
    store.close()


def test_unquoted_answer_right_after_the_bot_joins_its_thread(tmp_path: Path) -> None:
    store, registry = _setup(tmp_path)
    started = _say(store, registry, _event("q", mentioned=True, at_ms=T0))
    _bot(store, started, at_ms=T0 + 5_000, message_id="bot-1")

    answer = _say(store, registry, _event("a", at_ms=T0 + 300_000))

    assert answer.thread_id == started.thread_id
    assert answer.continuity_kind == "adjacent_bot_reply"
    store.close()


def test_outside_the_follow_up_window_nothing_joins(tmp_path: Path) -> None:
    store, registry = _setup(tmp_path)
    started = _say(store, registry, _event("q", mentioned=True, at_ms=T0))
    _bot(store, started, at_ms=T0 + 5_000, message_id="bot-1")

    late = _say(store, registry, _event("a", at_ms=T0 + 11 * 60_000))

    assert late.thread_id != started.thread_id
    store.close()


def test_a_dormant_thread_is_never_a_loose_candidate(tmp_path: Path) -> None:
    store, registry = _setup(tmp_path)
    started = _say(store, registry, _event("q", mentioned=True, at_ms=T0))
    _bot(store, started, at_ms=T0 + 5_000, message_id="bot-1")
    store.close_turn(str(started.turn_id), now_ms=T0 + 6_000, state="closed")
    assert store.mark_dormant((str(started.thread_id),), T0 + 7_000, "test")

    follow = _say(store, registry, _event("a", at_ms=T0 + 8_000))

    assert follow.thread_id != started.thread_id
    store.close()


def test_two_people_with_interleaved_threads_each_join_only_their_own(tmp_path: Path) -> None:
    store, registry = _setup(tmp_path)
    owner = _say(store, registry, _event("o1", mentioned=True, at_ms=T0))
    _bot(store, owner, at_ms=T0 + 1_000, message_id="bot-o")
    alex = _say(store, registry, _event("a1", sender=ALEX, mentioned=True, at_ms=T0 + 2_000))
    _bot(store, alex, at_ms=T0 + 3_000, message_id="bot-a")

    alex_follow = _say(store, registry, _event("a2", sender=ALEX, at_ms=T0 + 4_000))
    owner_follow = _say(store, registry, _event("o2", at_ms=T0 + 5_000))

    assert alex_follow.thread_id == alex.thread_id
    assert owner_follow.thread_id != owner.thread_id, "Alex spoke last, not the bot for the owner"
    store.close()


def test_twin_journal_rows_count_as_one_previous_message(tmp_path: Path) -> None:
    store, registry = _setup(tmp_path)
    started = _say(store, registry, _event("q", mentioned=True, at_ms=T0))
    twin = _event("q#canonical", sender="owner-lid@lid", at_ms=T0, origin="whatsapp_canonical")
    _say(store, registry, twin, assign=False)

    previous = store.previous_chat_message(channel="whatsapp", chat_id=CHAT, before_ms=T0 + 1_000)

    assert previous is not None
    assert previous[2] == frozenset({OWNER, "owner-lid@lid"})
    assert started.thread_id in previous[3]
    store.close()


def test_a_quote_after_new_starts_a_new_thread_that_keeps_the_quote(tmp_path: Path) -> None:
    store, registry = _setup(tmp_path)
    started = _say(store, registry, _event("q", mentioned=True, at_ms=T0))
    _bot(store, started, at_ms=T0 + 1_000, message_id="bot-1")
    store.close_chat_threads(channel="whatsapp", chat_id=CHAT, now_ms=T0 + 2_000)

    quote = _say(store, registry, _event("r", at_ms=T0 + 3_000, reply_to="bot-1"))

    assert quote.thread_id != started.thread_id
    assert quote.quote_ref == "bot-1"
    store.close()
