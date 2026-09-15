"""Bounded opportunity scheduling: coalescing, fairness and non-blocking observers.

Deterministic async tests with events instead of real sleeps.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from yeoman_gateway.consciousness.opportunities import (
    OpportunityScheduler,
    opportunity_id_for,
)
from yeoman_gateway.processing.participation import ParticipationOpportunity

CHAT = "synthetic@g.us"
OTHER = "other@g.us"


#: Newly created opportunities in these tests are "now"; only the expiry test moves
#: the clock forward explicitly.
_NOW_MS = int(time.time() * 1000)


def _opportunity(
    *,
    chat_id: str = CHAT,
    sources: tuple[str, ...] = ("m1",),
    revision: int = 1,
    trigger: str = "inbound",
    epoch: int = 1,
    created_ms: int = _NOW_MS,
) -> ParticipationOpportunity:
    return ParticipationOpportunity(
        opportunity_id=opportunity_id_for(
            channel="whatsapp",
            chat_id=chat_id,
            activation_epoch=epoch,
            lane="production",
            source_event_ids=sources,
            observed_revision=revision,
        ),
        channel="whatsapp",
        chat_id=chat_id,
        trigger=trigger,  # type: ignore[arg-type]
        source_event_ids=sources,
        observed_revision=revision,
        activation_epoch=epoch,
        created_at_ms=created_ms,
    )


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    def __call__(self, disposition: str, opportunity: ParticipationOpportunity, detail: dict):
        self.events.append((disposition, opportunity.opportunity_id, dict(detail)))

    def names(self) -> list[str]:
        return [name for name, _id, _detail in self.events]


@pytest.mark.asyncio
async def test_offer_returns_while_the_handler_is_blocked() -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def handle(opportunity):
        entered.set()
        await release.wait()

    scheduler = OpportunityScheduler(
        handle=handle,
        max_pending_chats=2,
        max_concurrent_decisions=1,
        ttl_seconds=120,
        max_pending_source_refs=64,
        max_pending_source_bytes=16384,
        on_disposition=lambda *args: None,
    )
    await scheduler.start()
    try:
        assert scheduler.offer(_opportunity()) is True
        await asyncio.wait_for(entered.wait(), timeout=1)
        # The producer is never blocked by the running handler.
        assert scheduler.offer(_opportunity(sources=("m2",), revision=2)) is True
        assert not release.is_set()
    finally:
        release.set()
        await scheduler.stop()


@pytest.mark.asyncio
async def test_two_offers_for_one_chat_merge_into_one_handler_call() -> None:
    calls: list[tuple[str, ...]] = []
    done = asyncio.Event()

    release = asyncio.Event()
    done = asyncio.Event()

    async def blocking(opportunity):
        calls.append(tuple(opportunity.source_event_ids))
        await release.wait()
        done.set()

    scheduler = OpportunityScheduler(
        handle=blocking, max_concurrent_decisions=1, on_disposition=lambda *args: None
    )
    await scheduler.start()
    try:
        scheduler.offer(_opportunity(sources=("m1",), revision=1))
        await asyncio.sleep(0.05)  # let the first call start
        scheduler.offer(_opportunity(sources=("m2",), revision=2))
        scheduler.offer(_opportunity(sources=("m3",), revision=3))
        assert scheduler.pending_count == 1
        release.set()
        await asyncio.wait_for(done.wait(), timeout=1)
    finally:
        release.set()
        await scheduler.stop()
    assert calls == [("m1",), ("m2", "m3")]


@pytest.mark.asyncio
async def test_duplicate_offer_of_the_same_material_is_not_rescheduled() -> None:
    recorder = _Recorder()
    calls: list[str] = []
    done = asyncio.Event()

    async def handle(opportunity):
        calls.append(opportunity.opportunity_id)
        done.set()

    scheduler = OpportunityScheduler(handle=handle, on_disposition=recorder)
    await scheduler.start()
    try:
        assert scheduler.offer(_opportunity(sources=("m1",), revision=1)) is True
        await asyncio.wait_for(done.wait(), timeout=1)
        done.clear()
        await asyncio.sleep(0.02)  # let the worker retire the finished record
        # The same trigger again over the same watermark: no new decision.
        assert scheduler.offer(_opportunity(sources=("m1",), revision=1, trigger="burst")) is False
        await asyncio.sleep(0.05)
    finally:
        await scheduler.stop()
    assert len(calls) == 1
    assert "duplicate" in recorder.names()


@pytest.mark.asyncio
async def test_trigger_type_is_metadata_not_a_second_identity() -> None:
    """The same sources never run twice just because burst and inbound both fired."""
    inbound = _opportunity(sources=("m1", "m2"), revision=4, trigger="inbound")
    burst = _opportunity(sources=("m2", "m1"), revision=4, trigger="burst")
    assert inbound.opportunity_id == burst.opportunity_id


@pytest.mark.asyncio
async def test_two_chats_progress_with_concurrency_two() -> None:
    started = asyncio.Event()
    both = asyncio.Event()
    release = asyncio.Event()
    seen: set[str] = set()

    async def handle(opportunity):
        seen.add(opportunity.chat_id)
        if len(seen) == 2:
            both.set()
        started.set()
        await release.wait()

    scheduler = OpportunityScheduler(handle=handle, max_concurrent_decisions=2)
    await scheduler.start()
    try:
        scheduler.offer(_opportunity(chat_id=CHAT))
        scheduler.offer(_opportunity(chat_id=OTHER))
        await asyncio.wait_for(both.wait(), timeout=2)
        assert seen == {CHAT, OTHER}
    finally:
        release.set()
        await scheduler.stop()


@pytest.mark.asyncio
async def test_one_chat_never_has_overlapping_handlers() -> None:
    concurrent = 0
    max_concurrent = 0
    calls = 0

    async def handle(opportunity):
        nonlocal concurrent, max_concurrent, calls
        calls += 1
        concurrent += 1
        max_concurrent = max(max_concurrent, concurrent)
        await asyncio.sleep(0.02)
        concurrent -= 1

    scheduler = OpportunityScheduler(handle=handle, max_concurrent_decisions=3)
    await scheduler.start()
    try:
        for revision in range(1, 6):
            scheduler.offer(_opportunity(sources=(f"m{revision}",), revision=revision))
            await asyncio.sleep(0)
        await asyncio.sleep(0.3)
    finally:
        await scheduler.stop()
    assert max_concurrent == 1
    assert calls >= 1


@pytest.mark.asyncio
async def test_shutdown_awaits_running_handlers() -> None:
    cancelled = asyncio.Event()

    async def handle(opportunity):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    scheduler = OpportunityScheduler(handle=handle, max_concurrent_decisions=1)
    await scheduler.start()
    scheduler.offer(_opportunity())
    await asyncio.sleep(0.05)
    await asyncio.wait_for(scheduler.stop(), timeout=2)
    assert cancelled.is_set()
    assert scheduler.running is False


@pytest.mark.asyncio
async def test_stale_candidate_is_dropped_at_dequeue() -> None:
    recorder = _Recorder()
    clock = {"now": _NOW_MS}
    calls: list[str] = []

    async def handle(opportunity):
        calls.append(opportunity.opportunity_id)

    scheduler = OpportunityScheduler(
        handle=handle,
        ttl_seconds=10,
        on_disposition=recorder,
        clock_ms=lambda: clock["now"],
    )
    # Not started yet: the candidate waits, then time passes beyond its TTL.
    assert scheduler.offer(_opportunity(created_ms=_NOW_MS)) is True
    clock["now"] = _NOW_MS + 60_000
    await scheduler.start()
    try:
        for _ in range(50):
            if "expired" in recorder.names():
                break
            await asyncio.sleep(0.01)
    finally:
        await scheduler.stop()
    assert calls == []
    assert "expired" in recorder.names()


@pytest.mark.asyncio
async def test_pending_chat_capacity_overflow_is_explicit() -> None:
    recorder = _Recorder()
    release = asyncio.Event()

    async def handle(opportunity):
        await release.wait()

    scheduler = OpportunityScheduler(
        handle=handle, max_pending_chats=1, max_concurrent_decisions=1, on_disposition=recorder
    )
    await scheduler.start()
    try:
        assert scheduler.offer(_opportunity(chat_id=CHAT)) is True
        await asyncio.sleep(0.05)
        assert scheduler.offer(_opportunity(chat_id=OTHER)) is False
    finally:
        release.set()
        await scheduler.stop()
    assert "queue_full" in recorder.names()


@pytest.mark.asyncio
async def test_source_reference_bounds_hold_and_report_drops() -> None:
    recorder = _Recorder()
    release = asyncio.Event()
    seen: list[tuple[str, ...]] = []

    async def handle(opportunity):
        seen.append(tuple(opportunity.source_event_ids))
        await release.wait()

    scheduler = OpportunityScheduler(
        handle=handle,
        max_pending_source_refs=3,
        max_pending_source_bytes=10_000,
        on_disposition=recorder,
        max_concurrent_decisions=1,
    )
    await scheduler.start()
    try:
        scheduler.offer(_opportunity(sources=("m0",), revision=0))
        await asyncio.sleep(0.05)  # first call takes m0
        for index in range(1, 50):
            scheduler.offer(_opportunity(sources=(f"m{index}",), revision=index))
        release.set()
        await asyncio.sleep(0.2)
    finally:
        release.set()
        await scheduler.stop()
    merged = seen[1] if len(seen) > 1 else ()
    assert len(merged) <= 3
    # Newest references are retained when the bound bites.
    assert "m49" in merged


@pytest.mark.asyncio
async def test_byte_budget_bounds_encoded_references() -> None:
    release = asyncio.Event()

    async def handle(opportunity):
        await release.wait()

    scheduler = OpportunityScheduler(
        handle=handle,
        max_pending_source_refs=1000,
        max_pending_source_bytes=64,
        max_concurrent_decisions=1,
    )
    await scheduler.start()
    try:
        scheduler.offer(_opportunity(sources=("seed",), revision=0))
        await asyncio.sleep(0.05)
        for index in range(200):
            scheduler.offer(_opportunity(sources=("x" * 40 + str(index),), revision=index + 1))
        pending = scheduler._pending.get(("whatsapp", CHAT))
        assert pending is not None
        assert pending.source_bytes <= 64
        assert pending.dropped_refs > 0
    finally:
        release.set()
        await scheduler.stop()


@pytest.mark.asyncio
async def test_cancel_chat_drops_pending_speculation() -> None:
    recorder = _Recorder()
    release = asyncio.Event()
    calls: list[tuple[str, ...]] = []

    async def handle(opportunity):
        calls.append(tuple(opportunity.source_event_ids))
        await release.wait()

    scheduler = OpportunityScheduler(
        handle=handle, max_concurrent_decisions=1, on_disposition=recorder
    )
    await scheduler.start()
    try:
        scheduler.offer(_opportunity(sources=("m1",), revision=1))
        await asyncio.sleep(0.05)
        scheduler.offer(_opportunity(sources=("m2",), revision=2))
        assert scheduler.cancel_chat("whatsapp", CHAT, reason="direct_request") is True
        # A new offer while cancelled is refused, not queued.
        assert scheduler.offer(_opportunity(sources=("m3",), revision=3)) is False
        scheduler.release_chat("whatsapp", CHAT)
        assert scheduler.offer(_opportunity(sources=("m3",), revision=3)) is True
        release.set()
        await asyncio.sleep(0.1)
    finally:
        release.set()
        await scheduler.stop()
    assert calls[0] == ("m1",)
    assert "cancelled" in recorder.names()


@pytest.mark.asyncio
async def test_epoch_change_replaces_instead_of_merging() -> None:
    recorder = _Recorder()
    release = asyncio.Event()

    async def handle(opportunity):
        await release.wait()

    scheduler = OpportunityScheduler(
        handle=handle, max_concurrent_decisions=1, on_disposition=recorder
    )
    await scheduler.start()
    try:
        scheduler.offer(_opportunity(sources=("m1",), revision=1, epoch=1))
        await asyncio.sleep(0.05)
        scheduler.offer(_opportunity(sources=("m2",), revision=2, epoch=2))
        assert "cancelled" in recorder.names()
    finally:
        release.set()
        await scheduler.stop()


@pytest.mark.asyncio
async def test_handler_failure_does_not_stop_the_pool() -> None:
    recorder = _Recorder()
    done = asyncio.Event()

    async def handle(opportunity):
        if opportunity.chat_id == CHAT:
            raise RuntimeError("boom")
        done.set()

    scheduler = OpportunityScheduler(handle=handle, on_disposition=recorder)
    await scheduler.start()
    try:
        scheduler.offer(_opportunity(chat_id=CHAT))
        await asyncio.sleep(0.05)
        scheduler.offer(_opportunity(chat_id=OTHER))
        await asyncio.wait_for(done.wait(), timeout=2)
    finally:
        await scheduler.stop()
    assert "handler_failed" in recorder.names()


@pytest.mark.asyncio
async def test_empty_source_offer_is_not_work() -> None:
    recorder = _Recorder()
    calls: list[str] = []

    async def handle(opportunity):
        calls.append(opportunity.opportunity_id)

    scheduler = OpportunityScheduler(handle=handle, on_disposition=recorder)
    await scheduler.start()
    try:
        assert scheduler.offer(_opportunity(sources=())) is False
        await asyncio.sleep(0.05)
    finally:
        await scheduler.stop()
    assert calls == []
    assert "empty" in recorder.names()


def test_opportunity_identity_excludes_trigger_and_includes_epoch_and_lane() -> None:
    base = dict(
        channel="whatsapp",
        chat_id=CHAT,
        activation_epoch=1,
        lane="production",
        source_event_ids=("m1", "m2"),
        observed_revision=3,
    )
    assert opportunity_id_for(**base) == opportunity_id_for(**{**base, "source_event_ids": ("m2", "m1")})
    assert opportunity_id_for(**base) != opportunity_id_for(**{**base, "activation_epoch": 2})
    assert opportunity_id_for(**base) != opportunity_id_for(**{**base, "lane": "shadow"})
    assert opportunity_id_for(**base) != opportunity_id_for(
        **{**base, "source_event_ids": ("m1",)}
    )


@pytest.mark.asyncio
async def test_offer_of_many_duplicate_revisions_queues_once() -> None:
    release = asyncio.Event()
    calls = 0

    async def handle(opportunity):
        nonlocal calls
        calls += 1
        await release.wait()

    scheduler = OpportunityScheduler(handle=handle, max_concurrent_decisions=1)
    await scheduler.start()
    try:
        scheduler.offer(_opportunity(sources=("m1",), revision=1))
        await asyncio.sleep(0.05)
        for _ in range(500):
            scheduler.offer(_opportunity(sources=("m1",), revision=1))
        # Every duplicate is suppressed: nothing new is queued.
        assert scheduler.pending_count == 0 or scheduler._pending[
            ("whatsapp", CHAT)
        ].unreported() == []
        assert len(scheduler._ready) <= 1
        release.set()
        await asyncio.sleep(0.1)
    finally:
        release.set()
        await scheduler.stop()
    assert calls == 1
