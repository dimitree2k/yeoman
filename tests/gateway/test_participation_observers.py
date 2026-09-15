"""Observer wiring: burst and lull become bounded opportunity producers (03.3).

Observers must publish and return. These tests use the real ``MessageBus`` event
dispatch path, so a regression that reintroduces model work inside a callback is
caught by an observable second-event delay rather than by inspection.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from yeoman_gateway.bus.events import InboundObservedEvent
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.consciousness.opportunities import OpportunityScheduler
from yeoman_gateway.consciousness.participation_runtime import (
    ParticipationRuntime,
    SourceOwner,
)
from yeoman_gateway.processing.participation import ParticipationOpportunity
from yeoman_shared.config.schema import Config, ConsciousnessConfig

CHAT = "synthetic@g.us"
OTHER = "other@g.us"

#: The lull observer compares its clock against event timestamps, so the events are
#: placed comfortably inside its activity window and before its silence threshold.
_LULL_NOW = 1_000_000_000.0
_LULL_EVENT_OFFSET = 300.0


def _eligible(channel: str, chat_id: str) -> bool:
    """The observers only ask yes/no here; the real check is policy + budget."""
    del channel, chat_id
    return True


def _config(**overrides: object) -> Config:
    payload = {
        "enabled": True,
        "burstEnabled": True,
        "burstThresholdMessages": 2,
        "burstWindowMinutes": 15,
        "lullEnabled": True,
        # A one-minute silence threshold keeps the observer's own timing out of the
        # test: what is under test is the producer callback, not the lull window.
        "lullSilenceMinutes": 1,
        "lullActivityWindowMinutes": 120,
        "lullMinRecentActivity": 2,
    }
    payload.update(overrides)
    return Config(consciousness=ConsciousnessConfig.model_validate(payload))


def _observed(timestamp: float, *, message_id: str = "m1") -> InboundObservedEvent:
    return InboundObservedEvent(
        channel="whatsapp",
        chat_id=CHAT,
        sender_id="anna@s.whatsapp.net",
        content="hello",
        timestamp=timestamp,
        message_id=message_id,
        is_group=True,
        metadata={"message_id": message_id},
    )


@pytest.mark.asyncio
async def test_event_dispatch_is_not_blocked_by_an_observer_producer(tmp_path: Path) -> None:
    """The real dispatch path does not sit in a model call inside a producer.

    A producer that awaited generation would keep the single dispatcher loop busy for
    seconds, and this test fails on the delivery deadline instead of on inspection.
    """
    import time as _time

    from yeoman_gateway.consciousness.burst import BurstObserver
    from yeoman_gateway.consciousness.log import SpeakupLog

    log = SpeakupLog(tmp_path / "speakups.db")
    owner = SourceOwner(store=log)
    handler_calls: list[str] = []

    async def handle(opportunity: ParticipationOpportunity) -> None:
        handler_calls.append(opportunity.opportunity_id)

    scheduler = OpportunityScheduler(handle=handle, max_concurrent_decisions=1, ttl_seconds=600)
    await scheduler.start()
    runtime = ParticipationRuntime(
        scheduler=scheduler, source_owner=owner, activation_epoch=1, is_enabled=lambda c, i: True
    )
    producer_done = asyncio.Event()

    async def offer(channel: str, chat_id: str) -> None:
        runtime.offer_source(
            channel=channel,
            chat_id=chat_id,
            source_event_ids=(f"observed-{chat_id}",),
            observed_revision=1,
            trigger="burst",
        )
        producer_done.set()

    burst = BurstObserver(
        config=_config(),
        state_path=tmp_path / "burst.json",
        on_burst=offer,
        is_eligible=_eligible,
    )
    bus = MessageBus()
    bus.subscribe_event("InboundObservedEvent", burst.handle)
    second_seen = asyncio.Event()

    async def harmless_handler(event: InboundObservedEvent) -> None:
        del event
        second_seen.set()

    bus.subscribe_event("InboundObservedEvent", harmless_handler)
    dispatcher = asyncio.create_task(bus.dispatch_events())
    try:
        await bus.publish_event(_observed(100.0, message_id="b1"))
        await bus.publish_event(_observed(101.0, message_id="b2"))
        started = _time.monotonic()
        await asyncio.wait_for(producer_done.wait(), timeout=2)
        produced_after = _time.monotonic() - started
        assert produced_after < 0.5, "the producer must not perform model work"
        await bus.publish_event(_observed(102.0, message_id="b3"))
        await asyncio.wait_for(second_seen.wait(), timeout=2)
    finally:
        bus.stop()
        dispatcher.cancel()
        with pytest.raises(asyncio.CancelledError):
            await dispatcher
        await scheduler.stop()
        log.close()
    assert handler_calls


@pytest.mark.asyncio
async def test_burst_and_lull_callbacks_return_without_model_work(tmp_path: Path) -> None:
    """With producers wired, an observer fire is a bounded offer, not a tick."""
    from yeoman_gateway.consciousness.burst import BurstObserver
    from yeoman_gateway.consciousness.lull import LullObserver

    owner_log_path = tmp_path / "speakups.db"
    from yeoman_gateway.consciousness.log import SpeakupLog

    log = SpeakupLog(owner_log_path)
    owner = SourceOwner(store=log)
    handled: list[ParticipationOpportunity] = []
    release = asyncio.Event()

    async def handle(opportunity: ParticipationOpportunity) -> None:
        handled.append(opportunity)
        await release.wait()

    scheduler = OpportunityScheduler(
        handle=handle, max_concurrent_decisions=1, ttl_seconds=600
    )
    await scheduler.start()
    runtime = ParticipationRuntime(
        scheduler=scheduler, source_owner=owner, activation_epoch=1, is_enabled=lambda c, i: True
    )
    burst = BurstObserver(
        config=_config(),
        state_path=tmp_path / "burst.json",
        on_burst=lambda channel, chat_id: runtime.offer_source(
            channel=channel,
            chat_id=chat_id,
            source_event_ids=(f"burst-{chat_id}",),
            observed_revision=1,
            trigger="burst",
        ),
        is_eligible=_eligible,
    )
    # A lull is a genuinely later revision with its own retained source, so it is a
    # new opportunity rather than a replay of the burst material.
    lull = LullObserver(
        config=_config(),
        state_path=tmp_path / "lull.json",
        on_lull=lambda channel, chat_id: runtime.offer_source(
            channel=channel,
            chat_id=chat_id,
            source_event_ids=(f"lull-{chat_id}",),
            observed_revision=9,
            trigger="lull",
        ),
        is_eligible=_eligible,
        clock=lambda: _LULL_NOW,
    )
    try:
        # Two messages reach the burst threshold and fire without awaiting the handler.
        await asyncio.wait_for(burst.handle(_observed(100.0, message_id="b1")), timeout=1)
        await asyncio.wait_for(burst.handle(_observed(101.0, message_id="b2")), timeout=1)
        await asyncio.sleep(0.05)
        assert len(handled) == 1
        assert handled[0].trigger == "burst"

        # Release the first evaluation so the next opportunity can be handled.
        release.set()
        await asyncio.sleep(0.05)
        release.clear()

        # The lull observer fires on its own schedule through its callback.
        await asyncio.wait_for(
            lull.handle(_observed(_LULL_NOW - _LULL_EVENT_OFFSET, message_id="b3")), timeout=1
        )
        await asyncio.wait_for(
            lull.handle(_observed(_LULL_NOW - _LULL_EVENT_OFFSET + 1, message_id="b4")), timeout=1
        )
        await asyncio.wait_for(lull._tick(), timeout=1)  # noqa: SLF001 - observer interval
        for _ in range(50):
            if len(handled) >= 2:
                break
            await asyncio.sleep(0.01)
        assert [item.trigger for item in handled] == ["burst", "lull"], (
            f"handled={len(handled)} counters={scheduler.counters()}"
        )
    finally:
        release.set()
        await scheduler.stop()
        log.close()


@pytest.mark.asyncio
async def test_observer_offer_is_never_a_second_production_owner(tmp_path: Path) -> None:
    """Burst and lull referring to the same activity produce one evaluation."""
    from yeoman_gateway.consciousness.log import SpeakupLog

    log = SpeakupLog(tmp_path / "speakups.db")
    owner = SourceOwner(store=log)
    handled: list[ParticipationOpportunity] = []
    release = asyncio.Event()

    async def handle(opportunity: ParticipationOpportunity) -> None:
        handled.append(opportunity)
        await release.wait()

    scheduler = OpportunityScheduler(handle=handle, max_concurrent_decisions=1, ttl_seconds=600)
    await scheduler.start()
    runtime = ParticipationRuntime(
        scheduler=scheduler, source_owner=owner, activation_epoch=1, is_enabled=lambda c, i: True
    )
    try:
        # The same retained source offered by three triggers is claimed once.
        for trigger in ("inbound", "burst", "lull"):
            runtime.offer_source(
                channel="whatsapp",
                chat_id=CHAT,
                source_event_ids=("m1",),
                observed_revision=1,
                trigger=trigger,
            )
        await asyncio.sleep(0.05)
        assert len(handled) == 1
        assert handled[0].trigger == "inbound"
    finally:
        release.set()
        await scheduler.stop()
    log.close()


@pytest.mark.asyncio
async def test_inbound_ingress_admits_new_material_only(tmp_path: Path) -> None:
    """Inbound messages reach the scheduler; our own output and tool traffic do not."""
    from yeoman_gateway.consciousness.log import SpeakupLog
    from yeoman_gateway.consciousness.participation_runtime import (
        ParticipationIngress,
        ParticipationRuntime,
    )

    log = SpeakupLog(tmp_path / "speakups.db")
    owner = SourceOwner(store=log)
    handled: list[str] = []
    release = asyncio.Event()

    async def handle(opportunity) -> None:
        handled.append(opportunity.trigger)
        await release.wait()

    scheduler = OpportunityScheduler(handle=handle, max_concurrent_decisions=1, ttl_seconds=600)
    await scheduler.start()
    runtime = ParticipationRuntime(
        scheduler=scheduler, source_owner=owner, activation_epoch=1, is_enabled=lambda c, i: True
    )
    ingress = ParticipationIngress(runtime=runtime, ledger=log, is_active=lambda c, i: True)
    try:
        assert ingress.handle_event(_observed(100.0, message_id="m1")) is True
        await asyncio.sleep(0.05)
        assert handled == ["inbound"]
        # A message with no durable identity, our own output, and tool traffic are all
        # refused: none of them is new human material to consider.
        assert ingress.handle_event(_observed(101.0, message_id="")) is False
        assert (
            ingress.handle_event(
                InboundObservedEvent(
                    channel="whatsapp",
                    chat_id=CHAT,
                    sender_id="arvid",
                    content="our own message",
                    timestamp=102.0,
                    message_id="m2",
                    is_group=True,
                    metadata={"participation": True},
                )
            )
            is False
        )
        assert (
            ingress.handle_event(
                InboundObservedEvent(
                    channel="whatsapp",
                    chat_id=CHAT,
                    sender_id="arvid",
                    content="tool traffic",
                    timestamp=103.0,
                    message_id="m3",
                    is_group=True,
                    metadata={"spawned_by_tool": True},
                )
            )
            is False
        )
    finally:
        release.set()
        await scheduler.stop()
    log.close()


@pytest.mark.asyncio
async def test_inbound_ingress_revisions_are_durable_and_monotonic(tmp_path: Path) -> None:
    from yeoman_gateway.consciousness.log import SpeakupLog

    log = SpeakupLog(tmp_path / "speakups.db")
    first = log.next_source_revision_sync(channel="whatsapp", chat_id=CHAT)
    second = log.next_source_revision_sync(channel="whatsapp", chat_id=CHAT)
    other = log.next_source_revision_sync(channel="whatsapp", chat_id=OTHER)
    assert (first, second, other) == (1, 2, 1)
    log.close()
    reopened = SpeakupLog(tmp_path / "speakups.db")
    # A restart never hands out a smaller revision for the same chat.
    assert reopened.next_source_revision_sync(channel="whatsapp", chat_id=CHAT) == 3
    reopened.close()


@pytest.mark.asyncio
async def test_ingress_event_handler_is_awaitable_and_never_breaks_dispatch(
    tmp_path: Path,
) -> None:
    """The bus awaits every handler: a non-coroutine handler aborts the whole dispatch.

    This is not hypothetical - a synchronous handler in this position raised
    "'bool' object can't be awaited" in the live gateway and skipped every handler
    registered after it (the legacy observers).
    """
    import inspect

    from yeoman_gateway.consciousness.log import SpeakupLog
    from yeoman_gateway.consciousness.participation_runtime import (
        ParticipationIngress,
        ParticipationRuntime,
    )

    log = SpeakupLog(tmp_path / "speakups.db")
    owner = SourceOwner(store=log)
    seen: list[str] = []

    async def handle(opportunity) -> None:
        seen.append(opportunity.trigger)

    scheduler = OpportunityScheduler(handle=handle, max_concurrent_decisions=1, ttl_seconds=600)
    await scheduler.start()
    runtime = ParticipationRuntime(
        scheduler=scheduler, source_owner=owner, activation_epoch=1, is_enabled=lambda c, i: True
    )
    ingress = ParticipationIngress(runtime=runtime, ledger=log, is_active=lambda c, i: True)

    async def on_observed(event: object) -> None:
        ingress.handle_event(event)

    bus = MessageBus()
    bus.subscribe_event("InboundObservedEvent", on_observed)
    later: list[str] = []

    async def after(event: object) -> None:
        del event
        later.append("ran")

    bus.subscribe_event("InboundObservedEvent", after)
    try:
        assert inspect.iscoroutinefunction(on_observed)
        dispatcher = asyncio.create_task(bus.dispatch_events())
        try:
            await bus.publish_event(_observed(100.0, message_id="m1"))
            for _ in range(100):
                if later:
                    break
                await asyncio.sleep(0.01)
        finally:
            bus.stop()
            dispatcher.cancel()
            with pytest.raises(asyncio.CancelledError):
                await dispatcher
        # Both the producer and every later handler ran.
        assert later == ["ran"]
        assert seen == ["inbound"]
    finally:
        await scheduler.stop()
        log.close()


@pytest.mark.asyncio
async def test_ingress_failure_does_not_abort_event_dispatch(tmp_path: Path) -> None:
    """A producer that raises is logged, not allowed to break other handlers."""
    from yeoman_gateway.consciousness.log import SpeakupLog
    from yeoman_gateway.consciousness.participation_runtime import (
        ParticipationIngress,
        ParticipationRuntime,
    )

    log = SpeakupLog(tmp_path / "speakups.db")

    class _ExplodingRuntime:
        def offer_source(self, **kwargs: object) -> bool:
            raise RuntimeError("scheduler unavailable")

    ingress = ParticipationIngress(
        runtime=_ExplodingRuntime(),  # type: ignore[arg-type]
        ledger=log,
        is_active=lambda c, i: True,
    )

    async def on_observed(event: object) -> None:
        try:
            ingress.handle_event(event)
        except Exception:  # noqa: BLE001 - mirrors the bootstrap adapter
            pass

    bus = MessageBus()
    bus.subscribe_event("InboundObservedEvent", on_observed)
    ran: list[str] = []

    async def after(event: object) -> None:
        del event
        ran.append("yes")

    bus.subscribe_event("InboundObservedEvent", after)
    dispatcher = asyncio.create_task(bus.dispatch_events())
    try:
        await bus.publish_event(_observed(100.0, message_id="m1"))
        for _ in range(100):
            if ran:
                break
            await asyncio.sleep(0.01)
    finally:
        bus.stop()
        dispatcher.cancel()
        with pytest.raises(asyncio.CancelledError):
            await dispatcher
    assert ran == ["yes"]
    log.close()
