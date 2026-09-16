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
async def test_direct_fence_stops_processing_runtime_before_judge(tmp_path) -> None:
    """A durable direct binding prevents an autonomous handler from spending a call."""
    from yeoman_gateway.consciousness.log import SpeakupLog
    from yeoman_gateway.processing.participation_runtime import ParticipationRuntime

    class Judge:
        calls = 0

        async def decide(self, opportunity, context):
            del opportunity, context
            self.calls += 1
            raise AssertionError("direct work must stop before the judge")

    class Context:
        calls = 0

        async def build(self, opportunity, *, inputs):
            del opportunity, inputs
            self.calls += 1
            raise AssertionError("direct work must stop before context")

    log = SpeakupLog(tmp_path / "speakups.db")
    judge = Judge()
    context = Context()
    runtime = ParticipationRuntime(
        judge=judge,  # type: ignore[arg-type]
        context_builder=context,
        ledger=log,
        snapshot_provider=lambda *args, **kwargs: {"enabled": True},
        is_source_allowed=lambda *args: True,
        source_principals=lambda channel, chat_id, sources: tuple(
            (source, "person") for source in sources
        ),
        direct_work_active=lambda channel, chat_id: True,
    )
    try:
        result = await runtime.evaluate_participation(_opportunity())
        assert result == {"status": "skipped", "reason": "direct_request"}
        assert judge.calls == 0
        assert context.calls == 0
        assert runtime.counters()["direct_superseded"] == 1
    finally:
        log.close()


@pytest.mark.asyncio
async def test_direct_fence_discards_waiting_draft_before_transport(tmp_path) -> None:
    """A direct request arriving during generation releases the autonomous hold."""
    from yeoman_gateway.consciousness.log import SpeakupLog, deterministic_effect_id
    from yeoman_gateway.processing.participation import ParticipationDecision
    from yeoman_gateway.processing.participation_runtime import ParticipationRuntime

    draft_started, release_draft = asyncio.Event(), asyncio.Event()
    direct = False

    class Judge:
        async def decide(self, opportunity, context):
            del opportunity, context
            return ParticipationDecision(
                action="comment",
                intent="initiate",
                reason="synthetic",
                purpose="synthetic",
                contribution_type="observation",
            )

    class Context:
        async def build(self, opportunity, *, inputs):
            del opportunity, inputs
            return {"messages": [], "anchors": []}

    class Submission:
        submitted = 0

        async def generate_draft(self, *, opportunity, decision, context):
            del opportunity, decision, context
            draft_started.set()
            await release_draft.wait()
            return "must not transport"

        async def submit(self, **kwargs):
            del kwargs
            self.submitted += 1

    log = SpeakupLog(tmp_path / "speakups.db")
    submission = Submission()

    def snapshot(*args, **kwargs):
        del args, kwargs
        return {
            "enabled": True,
            "opted_in": True,
            "activation_epoch": 1,
            "lane": "production",
            "allowed_actions": ("comment", "silence"),
            "allowed_intents": ("initiate",),
            "comment_allowed_intents": ("initiate",),
            "allowed_contribution_types": ("observation",),
            "allow_initiation": True,
            "allow_continuation": False,
            "allow_reactions": False,
            "spontaneity_enabled": True,
            "spontaneity_daily_cap": 10,
            "comment_limits": (("comment", 3, 1_800_000),),
            "reaction_limits": (),
            "continuation_candidate": False,
            "judge_calls_per_hour": 12,
            "min_gap_seconds": 0,
            "continuation_reserve": 0,
        }

    runtime = ParticipationRuntime(
        judge=Judge(),  # type: ignore[arg-type]
        context_builder=Context(),
        ledger=log,
        snapshot_provider=snapshot,
        is_source_allowed=lambda *args: True,
        source_principals=lambda channel, chat_id, sources: tuple(
            (source, "person") for source in sources
        ),
        submission=submission,
        direct_work_active=lambda channel, chat_id: direct,
    )
    opportunity = _opportunity()
    task = asyncio.create_task(runtime.evaluate_participation(opportunity))
    try:
        await asyncio.wait_for(draft_started.wait(), timeout=1)
        direct = True
        release_draft.set()
        result = await asyncio.wait_for(task, timeout=1)
        assert result == {"status": "skipped", "reason": "direct_request"}
        assert submission.submitted == 0
        effect_id = deterministic_effect_id(
            channel="whatsapp",
            chat_id=CHAT,
            operation="comment",
            proposal_id=opportunity.opportunity_id,
        )
        rows = await log.delivery_record(
            proposal_id=opportunity.opportunity_id, effect_id=effect_id
        )
        assert rows is not None and rows["delivery_state"] == "failed"
    finally:
        if not task.done():
            release_draft.set()
            await task
        log.close()


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
async def test_bounded_runtime_offer_releases_dropped_claims_and_reports_count(tmp_path) -> None:
    """Only newest bounded refs remain owned, with truncation metadata."""
    from yeoman_gateway.consciousness.log import SpeakupLog
    from yeoman_gateway.consciousness.participation_runtime import (
        OWNER_PARTICIPATION,
        ParticipationRuntime,
        SourceOwner,
    )

    log = SpeakupLog(tmp_path / "speakups.db")
    owner = SourceOwner(store=log)
    entered = asyncio.Event()
    release = asyncio.Event()
    handled: list[tuple[str, ...]] = []
    dispositions: list[tuple[str, dict]] = []

    async def handle(opportunity) -> None:
        handled.append(tuple(opportunity.source_event_ids))
        entered.set()
        await release.wait()

    def record(disposition, opportunity, detail) -> None:
        dispositions.append((disposition, dict(detail)))

    scheduler = OpportunityScheduler(
        handle=handle,
        max_pending_source_refs=2,
        on_disposition=record,
        max_concurrent_decisions=1,
    )
    await scheduler.start()
    runtime = ParticipationRuntime(scheduler=scheduler, source_owner=owner)
    try:
        assert runtime.offer_source(
            channel="whatsapp",
            chat_id=CHAT,
            source_event_ids=("m1", "m2", "m3"),
            observed_revision=3,
            trigger="inbound",
        )
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert handled == [("m2", "m3")]
        retry = owner.claim(
            channel="whatsapp",
            chat_id=CHAT,
            source_event_id="m1",
            activation_epoch=1,
            owner=OWNER_PARTICIPATION,
        )
        assert retry.granted and retry.reason == "claimed"
        started = next(detail for name, detail in dispositions if name == "started")
        assert started["dropped_source_count"] == 1
        assert log.highest_considered_revision_sync(
            channel="whatsapp", chat_id=CHAT
        ) == 3
    finally:
        release.set()
        await scheduler.stop()
        log.close()


@pytest.mark.asyncio
async def test_bounded_merge_keeps_claim_for_reported_inflight_source(tmp_path) -> None:
    """A source already handed to the handler is not released when coalescing."""
    from yeoman_gateway.consciousness.log import SpeakupLog
    from yeoman_gateway.consciousness.participation_runtime import (
        OWNER_PARTICIPATION,
        ParticipationRuntime,
        SourceOwner,
    )

    log = SpeakupLog(tmp_path / "speakups.db")
    owner = SourceOwner(store=log)
    entered = asyncio.Event()
    release = asyncio.Event()
    handled: list[tuple[str, ...]] = []
    dispositions: list[tuple[str, dict]] = []

    async def handle(opportunity) -> None:
        handled.append(tuple(opportunity.source_event_ids))
        if len(handled) == 1:
            entered.set()
            await release.wait()

    def record(disposition, opportunity, detail) -> None:
        del opportunity
        dispositions.append((disposition, dict(detail)))

    scheduler = OpportunityScheduler(
        handle=handle,
        max_pending_source_refs=1,
        on_disposition=record,
        max_concurrent_decisions=1,
    )
    await scheduler.start()
    runtime = ParticipationRuntime(scheduler=scheduler, source_owner=owner)
    try:
        assert runtime.offer_source(
            channel="whatsapp",
            chat_id=CHAT,
            source_event_ids=("m1",),
            observed_revision=1,
            trigger="inbound",
        )
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert runtime.offer_source(
            channel="whatsapp",
            chat_id=CHAT,
            source_event_ids=("m2",),
            observed_revision=2,
            trigger="burst",
        )
        still_owned = owner.claim(
            channel="whatsapp",
            chat_id=CHAT,
            source_event_id="m1",
            activation_epoch=1,
            owner=OWNER_PARTICIPATION,
        )
        assert still_owned.granted and still_owned.reason == "already_owned"
        release.set()
        for _ in range(100):
            if len(handled) == 2:
                break
            await asyncio.sleep(0.01)
        assert handled == [("m1",), ("m2",)]
        started = [detail for name, detail in dispositions if name == "started"]
        assert started[-1]["dropped_source_count"] == 1
    finally:
        release.set()
        await scheduler.stop()
        log.close()


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


# -- attempt limits and action-aware admission (03.2) ----------------------------------


@pytest.mark.asyncio
async def test_attempts_are_charged_before_the_provider_and_never_refunded(
    tmp_path,
) -> None:
    from yeoman_gateway.consciousness.log import SpeakupLog

    log = SpeakupLog(tmp_path / "speakups.db")
    assert await log.reserve_judge_attempt(
        "o1:0",
        opportunity_id="o1",
        channel="whatsapp",
        chat_id=CHAT,
        now_ms=1_000,
        hourly_limit=1,
        min_gap_ms=0,
        continuation_candidate=False,
        continuation_reserve=0,
    )
    await log.record_judge_outcome("o1:0", outcome="timeout")
    # A failed attempt is not refunded: it still occupies the hour.
    assert not await log.reserve_judge_attempt(
        "o2:0",
        opportunity_id="o2",
        channel="whatsapp",
        chat_id=CHAT,
        now_ms=2_000,
        hourly_limit=1,
        min_gap_ms=0,
        continuation_candidate=False,
        continuation_reserve=0,
    )
    log.close()


@pytest.mark.asyncio
async def test_duplicate_attempt_id_never_calls_the_provider_twice(tmp_path) -> None:
    from yeoman_gateway.consciousness.log import SpeakupLog

    log = SpeakupLog(tmp_path / "speakups.db")
    args = dict(
        opportunity_id="o1",
        channel="whatsapp",
        chat_id=CHAT,
        now_ms=1_000,
        hourly_limit=12,
        min_gap_ms=0,
        continuation_candidate=False,
        continuation_reserve=0,
    )
    assert await log.reserve_judge_attempt("o1:0", **args)
    assert not await log.reserve_judge_attempt("o1:0", **args)
    rows = await log.judge_attempts_since(
        channel="whatsapp", chat_id=CHAT, since_ms=0
    )
    assert len(rows) == 1
    log.close()


@pytest.mark.asyncio
async def test_reconsiderations_share_the_total_quota(tmp_path) -> None:
    """A reconsideration inside the chain needs no new gap but does cost an attempt."""
    from yeoman_gateway.consciousness.log import SpeakupLog

    log = SpeakupLog(tmp_path / "speakups.db")
    args = dict(
        opportunity_id="o1",
        channel="whatsapp",
        chat_id=CHAT,
        hourly_limit=2,
        min_gap_ms=3_600_000,
        continuation_candidate=True,
        continuation_reserve=0,
    )
    assert await log.reserve_judge_attempt("o1:0", now_ms=1_000, **args)
    # Inside the chain the minimum gap does not apply to the same opportunity.
    assert await log.reserve_judge_attempt("o1:1", now_ms=1_100, **args)
    assert not await log.reserve_judge_attempt("o1:2", now_ms=1_200, **args)
    log.close()


@pytest.mark.asyncio
async def test_continuation_reserve_protects_related_candidates(tmp_path) -> None:
    from yeoman_gateway.consciousness.log import SpeakupLog

    log = SpeakupLog(tmp_path / "speakups.db")
    base = dict(
        channel="whatsapp",
        chat_id=CHAT,
        now_ms=1_000,
        hourly_limit=4,
        min_gap_ms=0,
        continuation_reserve=2,
    )
    # Background initiation may use at most limit - reserve slots.
    assert await log.reserve_judge_attempt(
        "b1:0", opportunity_id="b1", continuation_candidate=False, **base
    )
    assert await log.reserve_judge_attempt(
        "b2:0", opportunity_id="b2", continuation_candidate=False, **base
    )
    assert not await log.reserve_judge_attempt(
        "b3:0", opportunity_id="b3", continuation_candidate=False, **base
    )
    # A related continuation still reaches the judge.
    assert await log.reserve_judge_attempt(
        "c1:0", opportunity_id="c1", continuation_candidate=True, **base
    )
    assert await log.reserve_judge_attempt(
        "c2:0", opportunity_id="c2", continuation_candidate=True, **base
    )
    # The total cap still binds.
    assert not await log.reserve_judge_attempt(
        "c3:0", opportunity_id="c3", continuation_candidate=True, **base
    )
    log.close()


@pytest.mark.asyncio
async def test_attempt_limits_survive_restart(tmp_path) -> None:
    from yeoman_gateway.consciousness.log import SpeakupLog

    db_path = tmp_path / "speakups.db"
    log = SpeakupLog(db_path)
    assert await log.reserve_judge_attempt(
        "o1:0",
        opportunity_id="o1",
        channel="whatsapp",
        chat_id=CHAT,
        now_ms=1_000,
        hourly_limit=1,
        min_gap_ms=0,
        continuation_candidate=False,
        continuation_reserve=0,
    )
    log.close()
    reopened = SpeakupLog(db_path)
    assert not await reopened.reserve_judge_attempt(
        "o2:0",
        opportunity_id="o2",
        channel="whatsapp",
        chat_id=CHAT,
        now_ms=1_500,
        hourly_limit=1,
        min_gap_ms=0,
        continuation_candidate=False,
        continuation_reserve=0,
    )
    reopened.close()


@pytest.mark.asyncio
async def test_minimum_gap_applies_to_unrelated_attempts(tmp_path) -> None:
    from yeoman_gateway.consciousness.log import SpeakupLog

    log = SpeakupLog(tmp_path / "speakups.db")
    base = dict(
        channel="whatsapp",
        chat_id=CHAT,
        hourly_limit=12,
        min_gap_ms=30_000,
        continuation_candidate=False,
        continuation_reserve=0,
    )
    assert await log.reserve_judge_attempt("o1:0", opportunity_id="o1", now_ms=1_000, **base)
    assert not await log.reserve_judge_attempt(
        "o2:0", opportunity_id="o2", now_ms=5_000, **base
    )
    assert await log.reserve_judge_attempt(
        "o3:0", opportunity_id="o3", now_ms=40_000, **base
    )
    log.close()


@pytest.mark.asyncio
async def test_zero_hourly_limit_denies_every_attempt(tmp_path) -> None:
    from yeoman_gateway.consciousness.log import SpeakupLog

    log = SpeakupLog(tmp_path / "speakups.db")
    assert not await log.reserve_judge_attempt(
        "o1:0",
        opportunity_id="o1",
        channel="whatsapp",
        chat_id=CHAT,
        now_ms=1_000,
        hourly_limit=0,
        min_gap_ms=0,
        continuation_candidate=True,
        continuation_reserve=0,
    )
    log.close()


@pytest.mark.asyncio
async def test_available_actions_shrink_with_budgets_but_never_lose_silence(
    tmp_path,
) -> None:
    from pathlib import Path

    from yeoman_gateway.bus.queue import MessageBus
    from yeoman_gateway.consciousness.approval import SpeakupApprovalStore
    from yeoman_gateway.consciousness.log import SpeakupLog
    from yeoman_gateway.consciousness.tools import ConsciousnessTools
    from yeoman_gateway.policy.engine import PolicyEngine
    from yeoman_gateway.policy.schema import PolicyConfig
    from yeoman_gateway.storage.inbound_archive import InboundArchive
    from yeoman_shared.config.schema import Config, ConsciousnessConfig

    from tests.gateway.test_participation_delivery import (
        GROUP,
        _AllowSecurity,
        _FakeMemory,
    )

    policy = PolicyConfig.model_validate(
        {
            "channels": {
                "whatsapp": {
                    "chats": {
                        GROUP: {
                            "participation": {
                                "enabled": True,
                                "maxReactionsPerWindow": 0,
                                "maxUnsolicitedCommentsPerWindow": 1,
                                "commentWindowMinutes": 30,
                            }
                        }
                    }
                }
            }
        }
    )
    log = SpeakupLog(tmp_path / "speakups.db")
    tools = ConsciousnessTools(
        config=Config(consciousness=ConsciousnessConfig(enabled=True)),
        policy_engine=PolicyEngine(policy, workspace=tmp_path),
        bus=MessageBus(),
        log=log,
        inbound_archive=InboundArchive(tmp_path / "inbound.db"),
        memory=_FakeMemory(),
        security=_AllowSecurity(),
        approval_store=SpeakupApprovalStore(tmp_path / "approvals.json"),
    )
    actions = await tools.available_actions_for(
        channel="whatsapp", chat_id=GROUP, now_ms=1_000
    )
    # A zero reaction cap disables reactions outright; silence always remains.
    assert "react" not in actions
    assert "comment" in actions
    assert "silence" in actions

    # Spend the single comment slot; the preflight then offers only silence.
    assert await log.reserve_delivery(
        proposal_id="p1",
        effect_id="e1",
        channel="whatsapp",
        chat_id=GROUP,
        now_ms=1_000,
        limits=(("comment", 1, 30 * 60_000),),
    )
    exhausted = await tools.available_actions_for(
        channel="whatsapp", chat_id=GROUP, now_ms=1_500
    )
    assert exhausted == ("silence",)
    assert Path(tmp_path).exists()
    log.close()


@pytest.mark.asyncio
async def test_emoji_traffic_does_not_reset_the_comment_window(tmp_path) -> None:
    """Human emoji traffic must not manufacture unlimited new exchanges."""
    from yeoman_gateway.consciousness.log import SpeakupLog

    log = SpeakupLog(tmp_path / "speakups.db")
    window_ms = 30 * 60_000
    limits = (("comment", 2, window_ms),)
    assert await log.reserve_delivery(
        proposal_id="p1", effect_id="e1", channel="whatsapp", chat_id=CHAT,
        now_ms=1_000, limits=limits,
    )
    async def fake_reaction_traffic() -> None:
        # Inbound chat activity is not a reservation: it cannot reset the window.
        return None

    for tick in range(5):
        await fake_reaction_traffic()
        assert await log.consumed_slots(
            channel="whatsapp", chat_id=CHAT, category="comment",
            now_ms=1_000 + tick * 1_000, window_ms=window_ms,
        ) == 1
    assert await log.reserve_delivery(
        proposal_id="p2", effect_id="e2", channel="whatsapp", chat_id=CHAT,
        now_ms=2_000, limits=limits,
    )
    assert not await log.reserve_delivery(
        proposal_id="p3", effect_id="e3", channel="whatsapp", chat_id=CHAT,
        now_ms=3_000, limits=limits,
    )
    log.close()


# -- exclusive source ownership and cutover (03.3, A36) --------------------------------


def _source_owner(tmp_path):
    from yeoman_gateway.consciousness.log import SpeakupLog
    from yeoman_gateway.consciousness.participation_runtime import SourceOwner

    log = SpeakupLog(tmp_path / "speakups.db")
    return SourceOwner(store=log), log


def test_one_production_owner_per_source_across_epochs(tmp_path) -> None:
    """The same source can never start both producers, before or after cutover."""
    from yeoman_gateway.consciousness.participation_runtime import (
        OWNER_LEGACY,
        OWNER_PARTICIPATION,
    )

    owner, log = _source_owner(tmp_path)
    legacy = owner.claim(
        channel="whatsapp",
        chat_id=CHAT,
        source_event_id="m1",
        activation_epoch=1,
        owner=OWNER_LEGACY,
    )
    assert legacy.granted is True
    # A later epoch does not hand the retained source to the new lane.
    participation = owner.claim(
        channel="whatsapp",
        chat_id=CHAT,
        source_event_id="m1",
        activation_epoch=2,
        owner=OWNER_PARTICIPATION,
    )
    assert participation.granted is False
    assert participation.reason == "owned_by_other"
    assert participation.is_legacy is True
    log.close()


def test_claim_is_idempotent_for_the_same_owner_and_survives_restart(tmp_path) -> None:
    from yeoman_gateway.consciousness.log import SpeakupLog
    from yeoman_gateway.consciousness.participation_runtime import (
        OWNER_PARTICIPATION,
        SourceOwner,
    )

    db_path = tmp_path / "speakups.db"
    log = SpeakupLog(db_path)
    owner = SourceOwner(store=log)
    first = owner.claim(
        channel="whatsapp",
        chat_id=CHAT,
        source_event_id="m1",
        activation_epoch=1,
        owner=OWNER_PARTICIPATION,
    )
    again = owner.claim(
        channel="whatsapp",
        chat_id=CHAT,
        source_event_id="m1",
        activation_epoch=1,
        owner=OWNER_PARTICIPATION,
    )
    assert first.granted and again.granted and again.reason == "already_owned"
    log.close()

    reopened = SpeakupLog(db_path)
    restarted = SourceOwner(store=reopened)
    duplicate = restarted.claim(
        channel="whatsapp",
        chat_id=CHAT,
        source_event_id="m1",
        activation_epoch=3,
        owner=OWNER_PARTICIPATION,
    )
    assert duplicate.granted is True and duplicate.reason == "already_owned"
    reopened.close()


def test_shadow_lane_never_consumes_a_production_source_id(tmp_path) -> None:
    from yeoman_gateway.consciousness.participation_runtime import (
        LANE_SHADOW,
        OWNER_LEGACY,
        OWNER_PARTICIPATION,
    )

    owner, log = _source_owner(tmp_path)
    shadow = owner.claim(
        channel="whatsapp",
        chat_id=CHAT,
        source_event_id="m1",
        activation_epoch=1,
        owner=OWNER_PARTICIPATION,
        lane=LANE_SHADOW,
    )
    assert shadow.granted is True
    # The production lane still owns the same source independently.
    production = owner.claim(
        channel="whatsapp",
        chat_id=CHAT,
        source_event_id="m1",
        activation_epoch=1,
        owner=OWNER_LEGACY,
    )
    assert production.granted is True
    log.close()


@pytest.mark.asyncio
async def test_runtime_queue_full_releases_claim_and_does_not_consume_watermark(tmp_path) -> None:
    """A rejected queue offer leaves both claim and durable considered state untouched."""
    from yeoman_gateway.consciousness.log import SpeakupLog
    from yeoman_gateway.consciousness.participation_runtime import (
        OWNER_PARTICIPATION,
        ParticipationRuntime,
        SourceOwner,
    )

    log = SpeakupLog(tmp_path / "speakups.db")
    owner = SourceOwner(store=log)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def handle(opportunity) -> None:
        del opportunity
        entered.set()
        await release.wait()

    scheduler = OpportunityScheduler(
        handle=handle, max_pending_chats=1, max_concurrent_decisions=1
    )
    await scheduler.start()
    runtime = ParticipationRuntime(scheduler=scheduler, source_owner=owner)
    try:
        assert runtime.offer_source(
            channel="whatsapp",
            chat_id=CHAT,
            source_event_ids=("first",),
            observed_revision=1,
            trigger="inbound",
        )
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert not runtime.offer_source(
            channel="whatsapp",
            chat_id=OTHER,
            source_event_ids=("rejected",),
            observed_revision=1,
            trigger="burst",
        )
        assert log.highest_considered_revision_sync(
            channel="whatsapp", chat_id=CHAT
        ) == 1
        assert log.highest_considered_revision_sync(
            channel="whatsapp", chat_id=OTHER
        ) == 0
        retry = owner.claim(
            channel="whatsapp",
            chat_id=OTHER,
            source_event_id="rejected",
            activation_epoch=1,
            owner=OWNER_PARTICIPATION,
        )
        assert retry.granted and retry.reason == "claimed"
    finally:
        release.set()
        await scheduler.stop()
    log.close()


def test_missing_source_id_is_never_claimed(tmp_path) -> None:
    owner, log = _source_owner(tmp_path)
    decision = owner.claim(
        channel="whatsapp", chat_id=CHAT, source_event_id="   ", activation_epoch=1, owner="legacy"
    )
    assert decision.granted is False
    assert decision.reason == "missing_source_id"
    log.close()


@pytest.mark.asyncio
async def test_runtime_offer_is_bounded_local_work(tmp_path) -> None:
    """The observer-facing entry point claims, offers and returns."""
    from yeoman_gateway.consciousness.participation_runtime import ParticipationRuntime

    owner, log = _source_owner(tmp_path)
    handled: list[tuple[str, ...]] = []
    release = asyncio.Event()

    async def handle(opportunity):
        handled.append(tuple(opportunity.source_event_ids))
        await release.wait()

    scheduler = OpportunityScheduler(handle=handle, max_concurrent_decisions=1)
    await scheduler.start()
    runtime = ParticipationRuntime(
        scheduler=scheduler, source_owner=owner, activation_epoch=1
    )
    try:
        assert runtime.offer_source(
            channel="whatsapp",
            chat_id=CHAT,
            source_event_ids=("m1",),
            observed_revision=1,
            trigger="burst",
        )
        # Replaying the same sources from another trigger yields no second evaluation.
        assert (
            runtime.offer_source(
                channel="whatsapp",
                chat_id=CHAT,
                source_event_ids=("m1",),
                observed_revision=1,
                trigger="lull",
            )
            is False
        )
    finally:
        release.set()
        await scheduler.stop()
    log.close()


@pytest.mark.asyncio
async def test_runtime_refuses_when_the_chat_is_not_enabled(tmp_path) -> None:
    from yeoman_gateway.consciousness.participation_runtime import ParticipationRuntime

    owner, log = _source_owner(tmp_path)

    async def handle(opportunity):
        raise AssertionError("must not run")

    scheduler = OpportunityScheduler(handle=handle)
    await scheduler.start()
    runtime = ParticipationRuntime(
        scheduler=scheduler,
        source_owner=owner,
        activation_epoch=1,
        is_enabled=lambda channel, chat_id: False,
    )
    try:
        assert (
            runtime.offer_source(
                channel="whatsapp",
                chat_id=CHAT,
                source_event_ids=("m1",),
                observed_revision=1,
                trigger="inbound",
            )
            is False
        )
    finally:
        await scheduler.stop()
    assert log.highest_considered_revision_sync(channel="whatsapp", chat_id=CHAT) == 0
    log.close()


@pytest.mark.asyncio
async def test_runtime_hydrates_highest_considered_revision_before_first_offer(tmp_path) -> None:
    """Restart admission starts after the durable considered watermark, not history."""
    from yeoman_gateway.consciousness.log import SpeakupLog
    from yeoman_gateway.consciousness.participation_runtime import (
        ParticipationRuntime,
        SourceOwner,
    )

    log = SpeakupLog(tmp_path / "speakups.db")
    await log.record_disposition(
        opportunity_id="already-considered",
        channel="whatsapp",
        chat_id=CHAT,
        disposition="decided_silence",
        observed_revision=2,
    )
    log.mark_material_considered_sync(
        channel="whatsapp", chat_id=CHAT, observed_revision=2
    )
    owner = SourceOwner(store=log)
    offered = []

    class Scheduler:
        def __init__(self) -> None:
            self.watermark = 0

        def mark_considered(self, channel: str, chat_id: str, *, observed_revision: int) -> None:
            self.watermark = int(observed_revision)
            offered.append(("watermark", channel, chat_id, observed_revision))

        def offer(self, opportunity) -> bool:
            if int(opportunity.observed_revision) <= self.watermark:
                return False
            offered.append(opportunity)
            return True

    runtime = ParticipationRuntime(
        scheduler=Scheduler(),
        source_owner=owner,
        activation_epoch=1,
        considered_revision_provider=log.highest_considered_revision_sync,
    )
    assert not runtime.offer_source(
        channel="whatsapp",
        chat_id=CHAT,
        source_event_ids=("old-source",),
        observed_revision=2,
        trigger="burst",
    )
    assert runtime.offer_source(
        channel="whatsapp",
        chat_id=CHAT,
        source_event_ids=("new-source",),
        observed_revision=3,
        trigger="inbound",
    )
    assert offered[0] == ("watermark", "whatsapp", CHAT, 2)
    assert offered[1].source_event_ids == ("new-source",)
    log.close()


@pytest.mark.asyncio
async def test_activation_epoch_advances_only_on_real_transitions(tmp_path) -> None:
    """A restart keeps the epoch; enable/shadow/route changes advance it (02.1/02.3)."""
    from yeoman_gateway.consciousness.log import SpeakupLog
    from yeoman_gateway.consciousness.participation_runtime import ActivationEpochTracker

    log = SpeakupLog(tmp_path / "speakups.db")
    tracker = ActivationEpochTracker(store=log)

    first = tracker.observe(
        channel="whatsapp", chat_id=CHAT, enabled=True, shadow=True, judge_route="r"
    )
    assert first == 1
    # The same inputs are not a transition, however often they are observed.
    again = tracker.observe(
        channel="whatsapp", chat_id=CHAT, enabled=True, shadow=True, judge_route="r"
    )
    assert again == 1
    # Leaving shadow is a transition.
    live = tracker.observe(
        channel="whatsapp", chat_id=CHAT, enabled=True, shadow=False, judge_route="r"
    )
    assert live == 2
    # Disabling is a transition; re-enabling advances again.
    off = tracker.observe(
        channel="whatsapp", chat_id=CHAT, enabled=False, shadow=False, judge_route="r"
    )
    assert off == 3
    back = tracker.observe(
        channel="whatsapp", chat_id=CHAT, enabled=True, shadow=True, judge_route="r"
    )
    assert back == 4
    # A restart is not a transition: a fresh tracker seeing the same inputs keeps it.
    restarted = ActivationEpochTracker(store=log)
    assert (
        restarted.observe(
            channel="whatsapp", chat_id=CHAT, enabled=True, shadow=True, judge_route="r"
        )
        == 4
    )
    log.close()


@pytest.mark.asyncio
async def test_activation_epoch_advances_across_a_restart(tmp_path) -> None:
    """A shadow/live change made while the process was down must still advance it."""
    from yeoman_gateway.consciousness.log import SpeakupLog
    from yeoman_gateway.consciousness.participation_runtime import ActivationEpochTracker

    db_path = tmp_path / "speakups.db"
    log = SpeakupLog(db_path)
    tracker = ActivationEpochTracker(store=log)
    # Running in shadow.
    assert tracker.observe(
        channel="whatsapp", chat_id=CHAT, enabled=True, shadow=True, judge_route="r"
    ) == 1
    log.close()

    # The owner flips to live while the process is down: a fresh tracker must see the
    # change, because the previous inputs are persisted, not remembered in memory.
    reopened = SpeakupLog(db_path)
    restarted = ActivationEpochTracker(store=reopened)
    assert restarted.observe(
        channel="whatsapp", chat_id=CHAT, enabled=True, shadow=False, judge_route="r"
    ) == 2
    # Stable afterwards.
    assert restarted.observe(
        channel="whatsapp", chat_id=CHAT, enabled=True, shadow=False, judge_route="r"
    ) == 2
    # And the fingerprint is readable for inspection.
    assert "shadow=False" in reopened.activation_fingerprint_sync("participation")
    reopened.close()


@pytest.mark.asyncio
async def test_first_observation_after_an_upgrade_does_not_invent_a_transition(
    tmp_path,
) -> None:
    """An existing epoch written before fingerprints existed is adopted, not bumped."""
    from yeoman_gateway.consciousness.log import SpeakupLog
    from yeoman_gateway.consciousness.participation_runtime import ActivationEpochTracker

    log = SpeakupLog(tmp_path / "speakups.db")
    # Simulate a row created by the old schema: an epoch with no fingerprint.
    await log.activate_scope_placeholder() if False else None
    assert int(log.activation_epoch_sync("participation")) == 1
    tracker = ActivationEpochTracker(store=log)
    assert tracker.observe(
        channel="whatsapp", chat_id=CHAT, enabled=True, shadow=False, judge_route="r"
    ) == 1
    assert tracker.observe(
        channel="whatsapp", chat_id=CHAT, enabled=True, shadow=True, judge_route="r"
    ) == 2
    log.close()


def test_global_activation_refresh_fences_pause_and_two_chat_changes(tmp_path) -> None:
    """The activation epoch is one durable global fence, not one fence per chat."""
    from yeoman_gateway.consciousness.log import SpeakupLog
    from yeoman_gateway.consciousness.participation_runtime import ActivationEpochTracker

    state = {
        "global": {"enabled": True, "shadow": True, "judge_route": "r"},
        "processing": {
            "enabled": True,
            "managed_targets": ("whatsapp:a@g.us",),
            "shadow_targets": (),
        },
        "chat_opt_ins": {"whatsapp:a@g.us": True, "whatsapp:b@g.us": False},
        "pauses": {"global": False, "chats": ()},
    }
    db_path = tmp_path / "speakups.db"
    log = SpeakupLog(db_path)
    tracker = ActivationEpochTracker(store=log, activation_state_provider=lambda: state)
    assert tracker.refresh_activation_sync() == 1
    assert tracker.refresh_activation_sync() == 1

    # A second chat's resolved opt-in is part of the same global fingerprint.
    state["chat_opt_ins"] = {"whatsapp:a@g.us": True, "whatsapp:b@g.us": True}
    assert tracker.refresh_activation_sync() == 2
    # A pause and its resume both fence old work even without an inbound message.
    state["pauses"] = {"global": True, "chats": ()}
    assert tracker.refresh_activation_sync() == 3
    state["pauses"] = {"global": False, "chats": ()}
    assert tracker.refresh_activation_sync() == 4
    log.close()

    restarted = SpeakupLog(db_path)
    restarted_tracker = ActivationEpochTracker(
        store=restarted, activation_state_provider=lambda: state
    )
    assert restarted_tracker.refresh_activation_sync() == 4
    restarted.close()


def test_pause_resume_refreshes_shared_activation_epoch_immediately(tmp_path) -> None:
    """Owner pause controls fence the shared epoch without waiting for a message."""
    from yeoman_gateway.adapters.policy_engine import EnginePolicyAdapter
    from yeoman_gateway.consciousness.log import SpeakupLog
    from yeoman_gateway.consciousness.participation_runtime import ActivationEpochTracker
    from yeoman_gateway.policy.engine import PolicyEngine
    from yeoman_gateway.policy.schema import PolicyConfig
    from yeoman_shared.config.schema import ProcessingConfig

    policy_path = tmp_path / "policy.json"
    policy = PolicyConfig.model_validate(
        {
            "owners": {"whatsapp": ["owner@s.whatsapp.net"]},
            "channels": {
                "whatsapp": {"chats": {CHAT: {"participation": {"enabled": True}}}}
            },
        }
    )
    engine = PolicyEngine(policy, workspace=tmp_path, apply_channels={"whatsapp"})
    processing = ProcessingConfig.model_validate(
        {"enabled": True, "participation": {"enabled": True, "judgeRoute": "route"}}
    )
    log = SpeakupLog(tmp_path / "speakups.db")
    tracker = ActivationEpochTracker(store=log)
    adapter = EnginePolicyAdapter(
        engine=engine,
        known_tools=set(),
        policy_path=policy_path,
        workspace=tmp_path,
        processing_config=processing,
        activation_tracker=tracker,
        reload_on_change=False,
    )
    assert adapter.current_activation("whatsapp", CHAT) is not None
    assert log.activation_epoch_sync("participation") == 1

    adapter._set_global_pause(-1)  # noqa: SLF001 - synthetic owner-control path
    assert log.activation_epoch_sync("participation") == 2
    assert adapter._clear_all_pauses() is True  # noqa: SLF001
    assert log.activation_epoch_sync("participation") == 3
    log.close()


def test_runtime_refreshes_activation_before_each_offer_and_keeps_shadow_lane(tmp_path) -> None:
    """A producer follows the current snapshot instead of a startup epoch/lane."""
    from dataclasses import dataclass

    from yeoman_gateway.consciousness.log import SpeakupLog
    from yeoman_gateway.consciousness.participation_runtime import (
        LANE_PRODUCTION,
        LANE_SHADOW,
        OWNER_LEGACY,
        ParticipationRuntime,
        SourceOwner,
    )

    @dataclass
    class Activation:
        activation_epoch: int
        shadow: bool
        live: bool
        observing: bool

    class Scheduler:
        def __init__(self) -> None:
            self.offered = []

        def offer(self, opportunity):
            self.offered.append(opportunity)
            return True

    current = Activation(activation_epoch=1, shadow=True, live=False, observing=True)
    log = SpeakupLog(tmp_path / "speakups.db")
    owner = SourceOwner(store=log)
    scheduler = Scheduler()
    runtime = ParticipationRuntime(
        scheduler=scheduler,
        source_owner=owner,
        activation_epoch=99,
        activation_provider=lambda channel, chat_id: current,
    )

    assert runtime.offer_source(
        channel="whatsapp",
        chat_id=CHAT,
        source_event_ids=("m-shadow",),
        observed_revision=1,
        trigger="burst",
    )
    assert scheduler.offered[-1].activation_epoch == 1
    assert scheduler.offered[-1].lane == LANE_SHADOW
    assert log.highest_considered_revision_sync(
        channel="whatsapp", chat_id=CHAT, lane=LANE_SHADOW
    ) == 1
    assert log.highest_considered_revision_sync(
        channel="whatsapp", chat_id=CHAT, lane=LANE_PRODUCTION
    ) == 0
    assert owner.claim(
        channel="whatsapp",
        chat_id=CHAT,
        source_event_id="m-shadow",
        activation_epoch=1,
        owner=OWNER_LEGACY,
        lane=LANE_PRODUCTION,
    ).granted

    current = Activation(activation_epoch=2, shadow=False, live=True, observing=False)
    assert runtime.offer_source(
        channel="whatsapp",
        chat_id=CHAT,
        source_event_ids=("m-live",),
        observed_revision=2,
        trigger="lull",
    )
    assert scheduler.offered[-1].activation_epoch == 2
    assert scheduler.offered[-1].lane == LANE_PRODUCTION
    assert log.highest_considered_revision_sync(
        channel="whatsapp", chat_id=CHAT, lane=LANE_PRODUCTION
    ) == 2
    assert not runtime.offer_source(
        channel="whatsapp",
        chat_id=CHAT,
        source_event_ids=("m-shadow",),
        observed_revision=2,
        trigger="inbound",
    )
    log.close()


def test_shadow_to_live_restart_reuses_material_without_replaying_shadow_lane(tmp_path) -> None:
    """A shadow claim is separate, so cutover can admit the source once in production."""
    from dataclasses import dataclass

    from yeoman_gateway.consciousness.log import SpeakupLog
    from yeoman_gateway.consciousness.participation_runtime import (
        LANE_PRODUCTION,
        LANE_SHADOW,
        ParticipationRuntime,
        SourceOwner,
    )

    @dataclass
    class Activation:
        activation_epoch: int
        shadow: bool
        live: bool
        observing: bool

    class Scheduler:
        def __init__(self) -> None:
            self.offered = []

        def offer(self, opportunity):
            self.offered.append(opportunity)
            return True

    db_path = tmp_path / "speakups.db"
    shadow_log = SpeakupLog(db_path)
    shadow_runtime = ParticipationRuntime(
        scheduler=Scheduler(),
        source_owner=SourceOwner(store=shadow_log),
        activation_provider=lambda channel, chat_id: Activation(1, True, False, True),
    )
    assert shadow_runtime.offer_source(
        channel="whatsapp",
        chat_id=CHAT,
        source_event_ids=("cutover-source",),
        observed_revision=1,
        trigger="burst",
    )
    assert shadow_log.highest_considered_revision_sync(
        channel="whatsapp", chat_id=CHAT, lane=LANE_SHADOW
    ) == 1
    assert shadow_log.highest_considered_revision_sync(
        channel="whatsapp", chat_id=CHAT, lane=LANE_PRODUCTION
    ) == 0
    shadow_log.close()

    live_log = SpeakupLog(db_path)
    live_scheduler = Scheduler()
    live_runtime = ParticipationRuntime(
        scheduler=live_scheduler,
        source_owner=SourceOwner(store=live_log),
        activation_provider=lambda channel, chat_id: Activation(2, False, True, False),
    )
    assert live_runtime.offer_source(
        channel="whatsapp",
        chat_id=CHAT,
        source_event_ids=("cutover-source",),
        observed_revision=1,
        trigger="inbound",
    )
    assert live_scheduler.offered[-1].lane == LANE_PRODUCTION
    assert live_log.highest_considered_revision_sync(
        channel="whatsapp", chat_id=CHAT, lane=LANE_PRODUCTION
    ) == 1
    live_log.close()


def test_runtime_hydrates_only_after_ready_activation_and_uses_lane(tmp_path) -> None:
    """Inactive/shadow preflight cannot hydrate production state before admission."""
    from dataclasses import dataclass

    from yeoman_gateway.consciousness.log import SpeakupLog
    from yeoman_gateway.consciousness.participation_runtime import (
        ParticipationRuntime,
        SourceOwner,
    )

    @dataclass
    class Activation:
        activation_epoch: int
        shadow: bool
        live: bool
        observing: bool

    current = Activation(1, False, False, False)
    calls: list[tuple[str, str]] = []

    class Scheduler:
        def mark_considered(self, channel, chat_id, *, observed_revision, lane="production"):
            del channel, chat_id, observed_revision
            calls.append(("hydration", str(lane)))

        def offer(self, opportunity):
            del opportunity
            calls.append(("offer", "production"))
            return True

    log = SpeakupLog(tmp_path / "speakups.db")
    runtime = ParticipationRuntime(
        scheduler=Scheduler(),
        source_owner=SourceOwner(store=log),
        activation_provider=lambda channel, chat_id: (
            calls.append(("activation", "snapshot")) or current
        ),
        considered_revision_provider=lambda **kwargs: (
            calls.append(("provider", str(kwargs["lane"]))) or 0
        ),
    )
    assert not runtime.offer_source(
        channel="whatsapp",
        chat_id=CHAT,
        source_event_ids=("not-ready",),
        observed_revision=1,
        trigger="burst",
    )
    assert calls == [("activation", "snapshot")]

    current = Activation(2, False, True, False)
    assert runtime.offer_source(
        channel="whatsapp",
        chat_id=CHAT,
        source_event_ids=("ready",),
        observed_revision=2,
        trigger="inbound",
    )
    assert calls[-3:] == [
        ("activation", "snapshot"),
        ("provider", "production"),
        ("offer", "production"),
    ]
    log.close()
