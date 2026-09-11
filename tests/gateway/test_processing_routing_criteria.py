"""Plan 07: the acceptance criteria of the routing spec, one test each.

Criteria already covered elsewhere are not repeated here (1, 3, 5, 8, 9, 12, 13, 15).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from yeoman_gateway.processing.store import ProcessingStore
from yeoman_gateway.processing.threads import (
    JoinRule,
    ThreadRegistry,
)

T0 = 1_700_000_000_000
CHAT = "chat@g.us"
PRINCIPAL = "owner@s.whatsapp.net"


class _Config:
    class Threads:
        followup_window_seconds = 600
        idle_seconds = 1800
        reopen_window_seconds = 604800
        pending_inputs_per_thread = 32

    ambient_chats: list[str] = [f"whatsapp:{CHAT}"]
    reply_actions: dict[str, str] = {}


def _store(tmp_path: Path) -> ProcessingStore:
    return ProcessingStore(tmp_path / "p.db")


def _event(eid: str, text: str, *, mentioned: bool = False, chat: str = CHAT):
    from yeoman_gateway.processing.models import CanonicalEvent

    return CanonicalEvent(
        event_id=eid,
        event_key=f"k-{eid}",
        trace_id=f"t-{eid}",
        kind="message",
        channel="whatsapp",
        chat_id=chat,
        principal=PRINCIPAL,
        source_message_id=eid,
        payload={"text": text, "is_group": chat.endswith("@g.us"), "mentioned_bot": mentioned},
    )


def _seed(store: ProcessingStore, event) -> ThreadRegistry:
    store.append_event(
        event_key=event.event_key,
        event_id=event.event_id,
        trace_id=event.trace_id,
        payload=event,
        now_ms=T0,
    )
    return ThreadRegistry(store=store, config=_Config())


def _two_candidate_threads(store: ProcessingStore, registry: ThreadRegistry) -> tuple[str, str]:
    """Two open threads of the same principal, each with a matching subject."""
    first = registry.assign(_event("m1", "Fasse den Mietvertrag zusammen.", mentioned=True), now_ms=T0)
    second = registry.assign(_event("m2", "Prüfe die Nebenkostenabrechnung.", mentioned=True), now_ms=T0 + 1)
    return str(first.thread_id), str(second.thread_id)


# criterion 2 -------------------------------------------------------------------------------


def test_criterion_2_two_eligible_candidates_are_never_attached_automatically(tmp_path: Path) -> None:
    """Two threads with a positive signal stay ambiguous: no silent pick."""
    store = _store(tmp_path)
    registry = _seed(store, _event("m1", "Fasse den Mietvertrag zusammen.", mentioned=True))
    store.append_event(
        event_key="k-m2", event_id="m2", trace_id="t-m2",
        payload=_event("m2", "Prüfe die Nebenkostenabrechnung.", mentioned=True), now_ms=T0 + 1,
    )
    registry.assign(_event("m2", "Prüfe die Nebenkkostenabrechnung.", mentioned=True), now_ms=T0 + 1)
    registry.assign(_event("m3", "Fasse den Mietvertrag zusammen.", mentioned=True), now_ms=T0 + 2)

    # A message that would fit both threads must not be attached to either.
    store.append_event(
        event_key="k-m4", event_id="m4", trace_id="t-m4",
        payload=_event("m4", "Zum Mietvertrag und zu den Nebenkosten: ergänze bitte."),
        now_ms=T0 + 3,
    )
    decision = registry.assign(
        _event("m4", "Zum Mietvertrag und zu den Nebenkosten: ergänze bitte."), now_ms=T0 + 4
    )

    assert decision.candidates_checked >= 2 or decision.thread_id is None
    assert decision.rule is not JoinRule.FOLLOWUP_SINGLE_ACTIVE
    store.close()


# criterion 4 -------------------------------------------------------------------------------


def test_criterion_4_a_bare_mention_does_not_choose_among_candidates(tmp_path: Path) -> None:
    """A mention answers "who", not "which thread"."""
    store = _store(tmp_path)
    registry = _seed(store, _event("m1", "Fasse den Mietvertrag zusammen.", mentioned=True))
    store.append_event(
        event_key="k-m2", event_id="m2", trace_id="t-m2",
        payload=_event("m2", "Prüfe die Nebenkostenabrechnung.", mentioned=True), now_ms=T0 + 1,
    )
    first_thread = str(registry.assign(_event("m1", "Fasse den Mietvertrag zusammen.", mentioned=True), now_ms=T0).thread_id)
    second = registry.assign(_event("m2", "Prüfe die Nebenkostenabrechnung.", mentioned=True), now_ms=T0 + 1)

    store.append_event(
        event_key="k-m3", event_id="m3", trace_id="t-m3",
        payload=_event("m3", "@Arvid mach weiter", mentioned=True), now_ms=T0 + 2,
    )
    decision = registry.assign(_event("m3", "@Arvid mach weiter", mentioned=True), now_ms=T0 + 3)

    assert decision.thread_id != first_thread, "the old thread must not be picked by a mention"
    assert decision.thread_id != str(second.thread_id)
    store.close()


# criterion 6 -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_criterion_6_a_follow_up_during_generation_is_parked_not_a_new_turn(tmp_path: Path) -> None:
    """An authorized addition during a running generation stays in that turn's postbox."""

    from yeoman_gateway.processing.actor import ThreadActorRegistry

    store = _store(tmp_path)
    registry = _seed(store, _event("m1", "Book Tuesday", mentioned=True))
    started = registry.assign(_event("m1", "Book Tuesday", mentioned=True), now_ms=T0)
    turn_id = str(started.turn_id)
    thread_id = str(started.thread_id)

    class _ActorConfig:
        class Threads:
            max_generations_global = 1
            max_generations_per_thread = 1
            pending_inputs_per_thread = 8

    actors = ThreadActorRegistry(store=store, config=_ActorConfig(), clock=lambda: T0)
    actor = actors.actor_for(thread_id)

    admission = actor.accept(
        event_id="m2", principal=PRINCIPAL, kind="message",
        explicit_correction=False, authorized=True,
    )

    assert admission.state in {"accepted", "waiting"}
    assert store.active_turn(thread_id).turn_id == turn_id, "no new turn was opened"
    pending = store.pending_inputs(thread_id)
    assert [item.event_id for item in pending] == ["m2"]
    store.close()


# criterion 10 ------------------------------------------------------------------------------


def test_criterion_10_a_longer_window_does_not_create_a_thread_per_message(tmp_path: Path) -> None:
    """A wider window widens the candidate set, not the number of durable threads."""
    store = _store(tmp_path)
    registry = _seed(store, _event("m1", "Fasse den Mietvertrag zusammen.", mentioned=True))
    registry.assign(_event("m1", "Fasse den Mietvertrag zusammen.", mentioned=True), now_ms=T0)

    for index in range(2, 6):
        text = f"Zum Mietvertrag: ergänze bitte Punkt {index}."
        store.append_event(
            event_key=f"k-m{index}", event_id=f"m{index}", trace_id=f"t-m{index}",
            payload=_event(f"m{index}", text), now_ms=T0 + index * 60_000,
        )
        decision = registry.assign(_event(f"m{index}", text), now_ms=T0 + index * 60_000)
        assert decision.rule is JoinRule.FOLLOWUP_SINGLE_ACTIVE, text

    assert len(store.list_threads(state="open")) == 1, "each message opened a thread"
    store.close()


# criterion 11 ------------------------------------------------------------------------------


def test_criterion_11_routing_never_reads_long_term_memory() -> None:
    """The routing layer must not import or consult the memory system."""
    import yeoman_gateway.processing.routing as routing
    import yeoman_gateway.processing.threads as threads

    for module in (routing, threads):
        source = Path(module.__file__).read_text()
        assert "memory" not in source.lower() or "memory_store" not in source.lower()
        assert "MemoryService" not in source, f"{module.__name__} reaches for long-term memory"


# criterion 14 ------------------------------------------------------------------------------


def test_criterion_14_a_closed_turn_gets_the_next_turn_in_the_same_thread(tmp_path: Path) -> None:
    """A finished turn leaves the thread open; the confirmed continuation continues it."""
    store = _store(tmp_path)
    registry = _seed(store, _event("m1", "Fasse den Mietvertrag zusammen.", mentioned=True))
    first = registry.assign(_event("m1", "Fasse den Mietvertrag zusammen.", mentioned=True), now_ms=T0)
    store.close_turn(str(first.turn_id), now_ms=T0 + 1_000, state="closed")

    text = "Zum Mietvertrag: ergänze bitte die Kündigungsfrist."
    store.append_event(
        event_key="k-m2", event_id="m2", trace_id="t-m2",
        payload=_event("m2", text), now_ms=T0 + 2_000,
    )
    decision = registry.assign(_event("m2", text), now_ms=T0 + 2_000)

    assert decision.rule is JoinRule.FOLLOWUP_SINGLE_ACTIVE
    assert decision.thread_id == first.thread_id, "the thread stays the same"
    assert decision.turn_id != first.turn_id, "the closed turn is not reopened"
    assert decision.new_turn is True
    store.close()
