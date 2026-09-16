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


def _material_provider(archive, log):
    """Compose pure archive ACL lookup with the durable ledger sequence."""

    def provide(channel: str, chat_id: str, source_ids: tuple[str, ...] | None):
        resolved = archive.resolve_source_ids(channel, chat_id, source_ids)
        if resolved:
            log.ensure_source_revisions_sync(
                channel=channel, chat_id=chat_id, source_ids=resolved
            )
        return log.material_for_opportunity(channel, chat_id, None if source_ids is None else resolved)

    return provide


def _synthetic_material_provider(
    channel: str, chat_id: str, source_ids: tuple[str, ...] | None
) -> tuple[tuple[str, ...], int]:
    """Explicit source composition for tests that do not construct an archive."""
    del channel, chat_id
    material = tuple(source_ids or ())
    return material, len(material)


def test_archive_material_resolution_is_pure_and_ledger_owns_revision(
    tmp_path: Path,
) -> None:
    from yeoman_gateway.consciousness.log import SpeakupLog
    from yeoman_gateway.storage.inbound_archive import InboundArchive

    archive = InboundArchive(tmp_path / "inbound.db", retention_days=None)
    for index, message_id in enumerate(("m1", "m2"), start=1):
        archive.record_inbound(
            channel="whatsapp",
            chat_id=CHAT,
            message_id=message_id,
            participant=f"person{index}@s.whatsapp.net",
            sender_id=f"person{index}",
            text=message_id,
            timestamp=index,
        )

    assert archive.resolve_source_ids(
        "whatsapp", CHAT, ("m2", "missing", "observed:whatsapp:" + CHAT, "m1")
    ) == ("m1", "m2")
    assert not archive._conn.execute(  # noqa: SLF001 - schema guard for the source owner
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'participation_material_state'"
    ).fetchone()

    log = SpeakupLog(tmp_path / "speakups.db")
    provider = _material_provider(archive, log)
    material, revision = provider(
        "whatsapp", CHAT, ("m2", "missing", "observed:whatsapp:" + CHAT, "m1")
    )
    assert material == ("m1", "m2")
    assert revision == 2
    assert log.highest_considered_revision_sync(channel="whatsapp", chat_id=CHAT) == 0
    log.mark_material_considered_sync(
        channel="whatsapp", chat_id=CHAT, observed_revision=revision
    )
    # The same source sequence remains the same after a later source appears.
    archive.record_inbound(
        channel="whatsapp",
        chat_id=CHAT,
        message_id="m3",
        participant="person3@s.whatsapp.net",
        sender_id="person3",
        text="m3",
        timestamp=3,
    )
    again, again_revision = provider("whatsapp", CHAT, ("m1",))
    assert again == ()
    assert again_revision == revision
    new_material, new_revision = provider("whatsapp", CHAT, None)
    assert new_material == ("m3",)
    assert new_revision == 3
    # Lookup alone does not advance the considered watermark, so an unaccepted
    # repeat still returns the same source. The runtime marks it after queue accept.
    assert provider("whatsapp", CHAT, None) == (("m3",), new_revision)
    log.mark_material_considered_sync(
        channel="whatsapp", chat_id=CHAT, observed_revision=new_revision
    )
    assert provider("whatsapp", CHAT, None) == ((), new_revision)
    log.close()
    archive.close()


def test_explicit_material_is_new_only_with_root_authorized_source_set(tmp_path: Path) -> None:
    """Explicit Root-authorized IDs cannot replay considered or add other ledger rows."""
    from yeoman_gateway.consciousness.log import SpeakupLog

    log = SpeakupLog(tmp_path / "speakups.db")
    log.ensure_source_revisions_sync(
        channel="whatsapp",
        chat_id=CHAT,
        source_ids=("authorized-old", "authorized-new", "blocked"),
    )
    log.mark_material_considered_sync(
        channel="whatsapp", chat_id=CHAT, observed_revision=1
    )

    assert log.material_for_opportunity(
        "whatsapp", CHAT, ("authorized-old", "authorized-new")
    ) == (("authorized-new",), 2)
    # Root's ACL-filtered source set does not contain the pre-registered blocked row.
    assert "blocked" not in log.material_for_opportunity(
        "whatsapp", CHAT, ("authorized-old", "authorized-new")
    )[0]
    log.close()


def test_first_enable_baselines_archive_history_but_restart_keeps_unconsidered_sources(
    tmp_path: Path,
) -> None:
    """Only a truly uninitialized ledger chat may adopt retained history as baseline."""
    from yeoman_gateway.consciousness.log import SpeakupLog
    from yeoman_gateway.storage.inbound_archive import InboundArchive

    archive = InboundArchive(tmp_path / "inbound.db", retention_days=None)
    archive.record_inbound(
        channel="whatsapp",
        chat_id=CHAT,
        message_id="historic",
        participant="person@s.whatsapp.net",
        sender_id="person",
        text="historic",
        timestamp=1,
    )
    log = SpeakupLog(tmp_path / "speakups.db")
    historic = archive.resolve_source_ids("whatsapp", CHAT, None)
    assert log.initialize_material_baseline_sync(
        channel="whatsapp", chat_id=CHAT, source_ids=historic
    ) == 1
    assert log.material_for_opportunity("whatsapp", CHAT, None) == ((), 1)
    assert log.material_for_opportunity(
        "whatsapp", CHAT, None, lane="shadow"
    ) == ((), 1)

    archive.record_inbound(
        channel="whatsapp",
        chat_id=CHAT,
        message_id="new",
        participant="person@s.whatsapp.net",
        sender_id="person",
        text="new",
        timestamp=2,
    )
    new_ids = archive.resolve_source_ids("whatsapp", CHAT, None)
    log.ensure_source_revisions_sync(
        channel="whatsapp", chat_id=CHAT, source_ids=new_ids
    )
    assert log.material_for_opportunity("whatsapp", CHAT, None) == (("new",), 2)

    pending_chat = "pending@g.us"
    log.ensure_source_revisions_sync(
        channel="whatsapp", chat_id=pending_chat, source_ids=("pending",)
    )
    # A restart/second baseline attempt cannot hide a registered but unconsidered id.
    assert log.initialize_material_baseline_sync(
        channel="whatsapp", chat_id=pending_chat, source_ids=("pending",)
    ) == 0
    log.ensure_source_revisions_sync(
        channel="whatsapp", chat_id=pending_chat, source_ids=("after-restart",)
    )
    assert log.material_for_opportunity("whatsapp", pending_chat, None) == (
        ("pending", "after-restart"),
        2,
    )
    log.close()
    archive.close()


def test_ledger_migrates_legacy_claims_and_dispositions_without_reallocating_on_restart(
    tmp_path: Path,
) -> None:
    """Legacy rows seed the ledger watermark once; a reopen remains idempotent."""
    import sqlite3

    from yeoman_gateway.consciousness.log import SpeakupLog

    db_path = tmp_path / "legacy-speakups.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE chat_revisions (
            channel TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 0,
            updated_at_ms INTEGER NOT NULL,
            PRIMARY KEY (channel, chat_id)
        );
        CREATE TABLE source_claims (
            channel TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            claim_key TEXT NOT NULL,
            owner TEXT NOT NULL,
            activation_epoch INTEGER NOT NULL,
            claimed_at_ms INTEGER NOT NULL,
            PRIMARY KEY (channel, chat_id, claim_key)
        );
        CREATE TABLE opportunity_dispositions (
            opportunity_id TEXT PRIMARY KEY,
            channel TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            observed_revision INTEGER NOT NULL DEFAULT 0,
            source_ids_json TEXT NOT NULL DEFAULT '[]',
            disposition TEXT NOT NULL,
            reason TEXT,
            created_at_ms INTEGER NOT NULL,
            updated_at_ms INTEGER NOT NULL
        );
        INSERT INTO chat_revisions VALUES ('whatsapp', 'legacy@g.us', 4, 1);
        INSERT INTO source_claims VALUES (
            'whatsapp', 'legacy@g.us', 'old-claim', 'legacy', 1, 2
        );
        INSERT INTO opportunity_dispositions VALUES (
            'old-opportunity', 'whatsapp', 'legacy@g.us', 5,
            '["old-disposition"]', 'decided_silence', 'legacy', 3, 3
        );
        """
    )
    conn.commit()
    conn.close()

    log = SpeakupLog(db_path)
    assert log.highest_considered_revision_sync(
        channel="whatsapp", chat_id="legacy@g.us"
    ) >= 5
    old_mapping = log.material_for_opportunity("whatsapp", "legacy@g.us", ("old-disposition",))
    assert old_mapping == ((), 6)
    created = log.ensure_source_revisions_sync(
        channel="whatsapp", chat_id="legacy@g.us", source_ids=("new",)
    )
    assert created and created[0][1] > 5
    highwater = log.highest_considered_revision_sync(
        channel="whatsapp", chat_id="legacy@g.us"
    )
    log.close()

    reopened = SpeakupLog(db_path)
    assert reopened.highest_considered_revision_sync(
        channel="whatsapp", chat_id="legacy@g.us"
    ) == highwater
    assert reopened.ensure_source_revisions_sync(
        channel="whatsapp", chat_id="legacy@g.us", source_ids=("new",)
    ) == tuple(created)
    reopened.close()


def test_archive_retention_does_not_renumber_ledger_source_revisions(tmp_path: Path) -> None:
    """Purging retained rows cannot move the durable source sequence backwards."""
    from yeoman_gateway.consciousness.log import SpeakupLog
    from yeoman_gateway.storage.inbound_archive import InboundArchive

    archive = InboundArchive(tmp_path / "inbound.db", retention_days=1)
    archive.record_inbound(
        channel="whatsapp",
        chat_id=CHAT,
        message_id="old",
        participant="person@s.whatsapp.net",
        sender_id="person",
        text="old",
        timestamp=1,
    )
    log = SpeakupLog(tmp_path / "speakups.db")
    log.ensure_source_revisions_sync(
        channel="whatsapp", chat_id=CHAT, source_ids=("old",)
    )
    log.mark_material_considered_sync(
        channel="whatsapp", chat_id=CHAT, observed_revision=1
    )
    with archive._lock:  # noqa: SLF001 - deterministic synthetic retention setup
        archive._conn.execute(  # noqa: SLF001
            "UPDATE inbound_messages SET created_at = ? WHERE message_id = ?",
            ("2000-01-01T00:00:00+00:00", "old"),
        )
        archive._conn.commit()  # noqa: SLF001
    assert archive.purge_older_than(1) == 1

    archive.record_inbound(
        channel="whatsapp",
        chat_id=CHAT,
        message_id="new",
        participant="person@s.whatsapp.net",
        sender_id="person",
        text="new",
        timestamp=2,
    )
    resolved = archive.resolve_source_ids("whatsapp", CHAT, None)
    assert resolved == ("new",)
    assert log.ensure_source_revisions_sync(
        channel="whatsapp", chat_id=CHAT, source_ids=resolved
    ) == (("new", 2),)
    assert log.material_for_opportunity("whatsapp", CHAT, None) == (("new",), 2)
    log.close()
    archive.close()


def test_ingress_uses_batch_material_provider_without_consuming_duplicate_revision(
    tmp_path: Path,
) -> None:
    from yeoman_gateway.consciousness.log import SpeakupLog
    from yeoman_gateway.consciousness.participation_runtime import ParticipationIngress
    from yeoman_gateway.storage.inbound_archive import InboundArchive

    archive = InboundArchive(tmp_path / "inbound.db", retention_days=None)
    for index, message_id in enumerate(("m1", "m2"), start=1):
        archive.record_inbound(
            channel="whatsapp",
            chat_id=CHAT,
            message_id=message_id,
            participant=f"person{index}@s.whatsapp.net",
            sender_id=f"person{index}",
            text=message_id,
            timestamp=index,
        )
    log = SpeakupLog(tmp_path / "speakups.db")

    class Runtime:
        def __init__(self) -> None:
            self.offers = []

        def offer_source(self, **kwargs: object) -> bool:
            self.offers.append(kwargs)
            return True

    runtime = Runtime()
    ingress = ParticipationIngress(
        runtime=runtime,  # type: ignore[arg-type]
        ledger=log,
        material_provider=_material_provider(archive, log),
        is_active=lambda channel, chat_id: True,
    )
    event = InboundObservedEvent(
        channel="whatsapp",
        chat_id=CHAT,
        sender_id="person2",
        content="m1 + m2",
        timestamp=2.0,
        message_id="m2",
        source_event_ids=("m1", "m2"),
        is_group=True,
    )
    assert ingress.handle_event(event)
    assert ingress.handle_event(event)
    assert [item["source_event_ids"] for item in runtime.offers] == [
        ("m1", "m2"),
        ("m1", "m2"),
    ]
    assert [item["observed_revision"] for item in runtime.offers] == [2, 2]
    # A trigger with no source material never increments the legacy revision table.
    empty = InboundObservedEvent(
        channel="whatsapp",
        chat_id=CHAT,
        sender_id="person2",
        content="",
        timestamp=2.0,
        message_id=None,
        source_event_ids=(),
        is_group=True,
    )
    assert ingress.handle_event(empty) is False
    assert log.activation_epoch_sync("participation") == 1
    log.close()
    archive.close()


def test_participation_ingress_rejects_trusted_direct_source_before_claim(
    tmp_path: Path,
) -> None:
    """Direct processing entries never become autonomous source claims."""
    from yeoman_gateway.consciousness.log import SpeakupLog
    from yeoman_gateway.consciousness.participation_runtime import ParticipationIngress

    log = SpeakupLog(tmp_path / "speakups.db")
    offered: list[object] = []

    class Runtime:
        def offer_source(self, **kwargs: object) -> bool:
            offered.append(kwargs)
            return True

    ingress = ParticipationIngress(
        runtime=Runtime(),  # type: ignore[arg-type]
        ledger=log,
        material_provider=_synthetic_material_provider,
        is_direct=lambda event: bool(
            (getattr(event, "metadata", {}) or {}).get("canonical_direct")
        ),
    )
    event = InboundObservedEvent(
        channel="whatsapp",
        chat_id=CHAT,
        sender_id="person",
        content="direct",
        timestamp=1.0,
        message_id="m-direct",
        source_event_ids=("m-direct",),
        metadata={"canonical_direct": True, "direct": False},
    )
    assert ingress.handle_event(event) is False
    assert offered == []
    # An untrusted metadata claim is not direct authority and follows the normal lane.
    allowed = InboundObservedEvent(
        channel="whatsapp",
        chat_id=CHAT,
        sender_id="person",
        content="social",
        timestamp=1.0,
        message_id="m-social",
        source_event_ids=("m-social",),
        metadata={"direct": True},
    )
    assert ingress.handle_event(allowed) is True
    assert len(offered) == 1
    log.close()


def test_participation_offer_is_fenced_while_direct_work_is_active(
    tmp_path: Path,
) -> None:
    """A direct binding blocks claims until its own terminal callback releases it."""
    from yeoman_gateway.consciousness.log import SpeakupLog
    from yeoman_gateway.consciousness.participation_runtime import (
        ParticipationRuntime,
        SourceOwner,
    )
    from yeoman_gateway.processing.store import ProcessingStore

    log = SpeakupLog(tmp_path / "speakups.db")
    log.ensure_source_revisions_sync(
        channel="whatsapp", chat_id=CHAT, source_ids=("m1",)
    )
    # The direct fence lives in the processing store in production; the Speakup ledger
    # test double exposes the same callback for this observer-only regression.
    direct_store = ProcessingStore(tmp_path / "processing.db")
    direct_store.note_direct_admission(
        channel="whatsapp", chat_id=CHAT, event_id="direct", turn_id="turn"
    )

    class Scheduler:
        def offer(self, opportunity: object) -> bool:
            del opportunity
            return True

    runtime = ParticipationRuntime(
        scheduler=Scheduler(),  # type: ignore[arg-type]
        source_owner=SourceOwner(store=log),
        direct_work_active=direct_store.direct_work_active,
    )
    try:
        assert runtime.offer_source(
            channel="whatsapp",
            chat_id=CHAT,
            source_event_ids=("m1",),
            observed_revision=1,
            trigger="inbound",
        ) is False
        direct_store.finish_direct_admission("turn")
        assert runtime.offer_source(
            channel="whatsapp",
            chat_id=CHAT,
            source_event_ids=("m1",),
            observed_revision=1,
            trigger="inbound",
        ) is True
    finally:
        direct_store.close()
        log.close()


@pytest.mark.asyncio
async def test_duplicate_batch_material_reaches_one_real_runtime_evaluation(
    tmp_path: Path,
) -> None:
    from yeoman_gateway.consciousness.log import SpeakupLog
    from yeoman_gateway.consciousness.participation_runtime import (
        ParticipationIngress,
        ParticipationRuntime,
    )
    from yeoman_gateway.storage.inbound_archive import InboundArchive

    archive = InboundArchive(tmp_path / "inbound.db", retention_days=None)
    for index, message_id in enumerate(("m1", "m2"), start=1):
        archive.record_inbound(
            channel="whatsapp",
            chat_id=CHAT,
            message_id=message_id,
            participant=f"person{index}@s.whatsapp.net",
            sender_id=f"person{index}",
            text=message_id,
            timestamp=index,
        )
    log = SpeakupLog(tmp_path / "speakups.db")
    owner = SourceOwner(store=log)
    handled: list[tuple[str, ...]] = []
    release = asyncio.Event()

    async def handle(opportunity: ParticipationOpportunity) -> None:
        handled.append(tuple(opportunity.source_event_ids))
        await release.wait()

    scheduler = OpportunityScheduler(handle=handle, max_concurrent_decisions=1, ttl_seconds=600)
    await scheduler.start()
    runtime = ParticipationRuntime(
        scheduler=scheduler,
        source_owner=owner,
        activation_epoch=1,
    )
    ingress = ParticipationIngress(
        runtime=runtime,
        ledger=log,
        material_provider=_material_provider(archive, log),
        is_active=lambda channel, chat_id: True,
    )
    event = InboundObservedEvent(
        channel="whatsapp",
        chat_id=CHAT,
        sender_id="person2",
        content="m1 + m2",
        timestamp=2.0,
        message_id="m2",
        source_event_ids=("m1", "m2"),
        is_group=True,
    )
    try:
        assert ingress.handle_event(event)
        assert not ingress.handle_event(event)
        for _ in range(100):
            if handled:
                break
            await asyncio.sleep(0.01)
        assert handled == [("m1", "m2")]
    finally:
        release.set()
        await scheduler.stop()
        log.close()
        archive.close()


@pytest.mark.asyncio
async def test_message_bus_preserves_batch_source_event_ids(tmp_path: Path) -> None:
    from yeoman_gateway.bus.events import InboundMessage

    bus = MessageBus()
    await bus.publish_inbound(
        InboundMessage(
            channel="whatsapp",
            sender_id="person",
            chat_id=CHAT,
            content="m1 + m2",
            metadata={"message_id": "m2", "source_event_ids": ["m1", "m2"]},
        )
    )
    observed = bus._event_queue.get_nowait()  # noqa: SLF001 - synthetic bus boundary
    assert observed.source_event_ids == ("m1", "m2")


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
    ingress = ParticipationIngress(
        runtime=runtime,
        ledger=log,
        material_provider=_synthetic_material_provider,
        is_active=lambda c, i: True,
    )
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
    ingress = ParticipationIngress(
        runtime=runtime,
        ledger=log,
        material_provider=_synthetic_material_provider,
        is_active=lambda c, i: True,
    )

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
    from yeoman_gateway.consciousness.participation_runtime import ParticipationIngress

    log = SpeakupLog(tmp_path / "speakups.db")

    class _ExplodingRuntime:
        def offer_source(self, **kwargs: object) -> bool:
            raise RuntimeError("scheduler unavailable")

    ingress = ParticipationIngress(
        runtime=_ExplodingRuntime(),  # type: ignore[arg-type]
        ledger=log,
        material_provider=_synthetic_material_provider,
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
