"""Native source registration: prove audiences from projected history, never a registry."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from pathlib import Path

import pytest
from yeoman_gateway.history.context import history_knowledge_scope
from yeoman_gateway.history.live import HistoryPaused
from yeoman_gateway.history.queries import HistoryQueries
from yeoman_gateway.knowledge import open_knowledge_store, workspace_id_for
from yeoman_gateway.knowledge._capture import ObservedSourceRegistrar
from yeoman_gateway.knowledge._source_catchup import ObservedSourceCatchup
from yeoman_gateway.knowledge.runtime import RuntimeKnowledgePolicy, RuntimeKnowledgeSources
from yeoman_gateway.policy.engine import PolicyEngine
from yeoman_gateway.policy.schema import PolicyConfig
from yeoman_gateway.processing.signals import SignalJournalSink
from yeoman_gateway.processing.store import ProcessingStore

from tests.gateway.history_test_helpers import (
    ProjectedHistory,
    prepare_history_retirement_knowledge,
)

NOW = 1_700_000_000_000
GROUP = "491786127564-1611913127@g.us"
DIRECT = "491757070305@s.whatsapp.net"
SENDER = "491757070305"
OTHER = "491511"


class _Runtime:
    """A Knowledge service and registrar under the real native-history scope."""

    def __init__(self, tmp_path: Path, *, history_members: tuple[str, ...] = ()) -> None:
        self.store = ProcessingStore(tmp_path / "processing.db")
        self.history = ProjectedHistory(tmp_path / "projected-history")
        async def read_turn():
            return self.history.snapshot()
        self.history.read_turn = read_turn
        self.members = tuple(history_members)
        engine = PolicyEngine(PolicyConfig.model_validate({
            "defaults": {"whoCanTalk": {"mode": "everyone"}},
            "channels": {"whatsapp": {"chats": {GROUP: {}}}},
        }), tmp_path)
        self.policy = RuntimeKnowledgePolicy(engine=engine, policy_revision=1)
        self.sources = RuntimeKnowledgeSources(processing_store=self.store)
        self.knowledge = open_knowledge_store(
            prepare_history_retirement_knowledge(tmp_path, "knowledge"),
            workspace_id=workspace_id_for(tmp_path),
            source_authority=self.sources,
            policy_authority=self.policy,
            clock=lambda: NOW,

        )
        self.registrar = ObservedSourceRegistrar(knowledge=self.knowledge, processing=self.store, clock=lambda: NOW)
        self.registration_results: dict[str, bool] = {}
        self.sink = SignalJournalSink(self.store, clock=lambda: NOW, sources=self.registrar)
        self.sink._history_projector = self.history
        self.sink._history_knowledge = self.knowledge

    @contextmanager
    def scope(self):
        snapshot = self.history.snapshot()
        try:
            with history_knowledge_scope(snapshot, self.knowledge):
                yield snapshot
        finally:
            snapshot.close()

    def project_message(self, *, chat_id: str, message_id: str, occurred_ms: int = NOW) -> None:
        if chat_id.endswith("@g.us") and self.members:
            self.history.add_group(
                chat_id=chat_id,
                participants=[f"{member}@s.whatsapp.net" for member in self.members],
                timestamp=occurred_ms,
            )
        identifiers = {SENDER, *self.members}
        for member in identifiers:
            self.history.add_contact(
                identifiers=[f"{member}@s.whatsapp.net"], name=f"Synthetic {member}", timestamp=occurred_ms
            )
        self.history.add_message(
            chat_id=chat_id, message_id=message_id, text="Wir treffen uns am Freitag um acht.",
            timestamp=occurred_ms, sender_id=f"{SENDER}@s.whatsapp.net",
        )

    def close(self) -> None:
        self.history.close()
        self.knowledge.close()
        self.store.close()


def _append_message(
    runtime: _Runtime, *, chat_id: str, message_id: str, occurred_ms: int = NOW
) -> str:
    runtime.project_message(chat_id=chat_id, message_id=message_id, occurred_ms=occurred_ms)
    payload = {
        "chatJid": chat_id, "messageId": message_id, "senderId": SENDER,
        "text": "Wir treffen uns am Freitag um acht.",
        "isGroup": chat_id.endswith("@g.us"), "timestamp": occurred_ms,
    }
    stored = runtime.sink.capture(
        "message", payload, event_id=f"wa_{message_id}",
        event_key=f"whatsapp:default:{chat_id}:message:{message_id}", account="default",
        observed_at_ms=NOW, strict=True,
    )
    runtime.registration_results[stored] = asyncio.run(
        runtime.sink.register_observed_source_live(stored)
    )
    assert stored == f"wa_{message_id}"
    return stored


def _source(runtime: _Runtime, chat_id: str, native_id: str, revision: int = 1):
    from yeoman_gateway.knowledge._history_sources import HistoryKnowledgeSources

    with runtime.scope() as snapshot:
        queries = HistoryQueries(snapshot)
        authority = HistoryKnowledgeSources(
            queries, {}, runtime.knowledge.history_source_ledger,
            runtime.knowledge._legacy_authority,
        )
        native = queries.native_message(chat_id=chat_id, native_id=native_id)
        assert native is not None
        source = authority.verify_source_ref(native["message_id"], revision)
        audience = (
            authority.evidence_audience(source, basis="observed_source_batch")
            if source is not None else None
        )
        return source, audience


def test_group_observation_registers_proven_native_audience_without_a_turn(tmp_path: Path) -> None:
    runtime = _Runtime(tmp_path, history_members=(SENDER, OTHER))
    try:
        event_id = _append_message(runtime, chat_id=GROUP, message_id="m1")
        source, audience = _source(runtime, GROUP, "m1")
        assert source is not None and source.author_principal == "whatsapp:491757070305"
        assert audience is not None and audience.status == "known"
        assert set(audience.members) == {"whatsapp:491757070305", "whatsapp:491511"}
        assert runtime.registration_results[event_id] is True
        assert runtime.store.get_event_source_authority(event_id)["audience_status"] == "unknown"
        assert runtime.store.count_events() == 1
    finally:
        runtime.close()


def test_direct_observation_is_proven_author_only(tmp_path: Path) -> None:
    runtime = _Runtime(tmp_path)
    try:
        _append_message(runtime, chat_id=DIRECT, message_id="m2")
        source, audience = _source(runtime, DIRECT, "m2")
        assert source is not None
        assert audience is not None and audience.status == "author_only"
        assert runtime.store.get_event_source_authority("wa_m2")["audience_status"] == "unknown"
    finally:
        runtime.close()


def test_group_without_native_membership_stays_durable_and_unknown(tmp_path: Path) -> None:
    runtime = _Runtime(tmp_path)
    try:
        event_id = _append_message(runtime, chat_id=GROUP, message_id="m3")
        assert runtime.store.get_event(event_id) is not None
        source, _ = _source(runtime, GROUP, "m3")
        assert source is None
        assert runtime.registration_results[event_id] is False
        assert runtime.store.get_event_source_authority(event_id)["audience_status"] == "unknown"
    finally:
        runtime.close()


def test_registration_uses_native_event_time_and_canonical_source(tmp_path: Path) -> None:
    runtime = _Runtime(tmp_path, history_members=(SENDER,))
    try:
        occurred_ms = NOW - 60_000
        _append_message(runtime, chat_id=GROUP, message_id="m4", occurred_ms=occurred_ms)
        issued, _ = _source(runtime, GROUP, "m4")
        assert issued is not None and issued.occurred_at_ms == occurred_ms
        assert issued.author_principal == "whatsapp:491757070305"
        assert runtime.store.count_events() == 1
    finally:
        runtime.close()


def test_unknown_event_is_never_registered(tmp_path: Path) -> None:
    runtime = _Runtime(tmp_path, history_members=(SENDER,))
    try:
        assert asyncio.run(runtime.sink.register_observed_source_live("does-not-exist")) is False
    finally:
        runtime.close()


def _catchup(runtime: _Runtime, **kwargs) -> ObservedSourceCatchup:
    return ObservedSourceCatchup(
        processing=runtime.store, knowledge=runtime.knowledge, registrar=runtime.registrar,
        projector=runtime.history, clock=lambda: NOW, **kwargs,
    )


@pytest.mark.asyncio
async def test_catchup_after_pause_uses_source_time_members_and_is_idempotent(tmp_path, monkeypatch):
    runtime = _Runtime(tmp_path, history_members=(SENDER, OTHER))
    try:
        async def paused():
            raise HistoryPaused("starting")

        monkeypatch.setattr(runtime.history, "read_turn", paused)
        # The synchronous fixture models the channel's first failed live registration.
        runtime.project_message(chat_id=GROUP, message_id="retry", occurred_ms=NOW - 1000)
        event_id = runtime.sink.capture(
            "message", {"chatJid": GROUP, "messageId": "retry", "senderId": SENDER,
                        "text": "Synthetic", "isGroup": True, "timestamp": NOW - 1000},
            event_id="wa_retry", event_key="retry", account="default",
            observed_at_ms=NOW, strict=True,
        )
        assert not await runtime.sink.register_observed_source_live(event_id)
        assert runtime.store.get_event_source_authority(event_id)["audience_status"] == "unknown"
        catchup = _catchup(runtime)
        paused_report = await catchup.run_once()
        assert paused_report.registered == 0
        assert paused_report.still_unknown == {"history_paused": 1}
        assert runtime.knowledge.history_source_ledger.current(f"whatsapp:{GROUP}:retry") is None

        async def ready():
            return runtime.history.snapshot()

        monkeypatch.setattr(runtime.history, "read_turn", ready)
        runtime.history.add_group(chat_id=GROUP, participants=[
            f"{SENDER}@s.whatsapp.net", f"{OTHER}@s.whatsapp.net", "491999@s.whatsapp.net",
        ], timestamp=NOW + 1000)
        report = await catchup.run_once()
        assert (report.examined, report.registered) == (1, 1)
        source, audience = _source(runtime, GROUP, "retry")
        assert source.occurred_at_ms == NOW - 1000
        assert audience.members == frozenset({f"whatsapp:{SENDER}", f"whatsapp:{OTHER}"})
        monkeypatch.setattr(runtime.registrar, "register", lambda _: pytest.fail("already proven"))
        second = await catchup.run_once()
        assert (second.registered, second.already_proven) == (0, 1)
    finally:
        runtime.close()


@pytest.mark.parametrize("chat_id,members,expected", [
    (DIRECT, (), "author_only"), (GROUP, (), None),
])
def test_catchup_direct_and_unknown_membership(tmp_path, monkeypatch, chat_id, members, expected):
    runtime = _Runtime(tmp_path, history_members=members)
    try:
        with monkeypatch.context() as patch:
            async def paused():
                raise HistoryPaused("starting")
            patch.setattr(runtime.history, "read_turn", paused)
            _append_message(runtime, chat_id=chat_id, message_id="audience")
        report = asyncio.run(_catchup(runtime).run_once())
        source, audience = _source(runtime, chat_id, "audience")
        if expected:
            assert audience.status == expected
            assert source.author_principal == f"whatsapp:{SENDER}"
            assert report.registered == 1
        else:
            assert source is None and audience is None
            assert report.still_unknown == {"source_proof_unavailable": 1}
    finally:
        runtime.close()


def test_catchup_missing_history_and_processing_revocation(tmp_path, monkeypatch):
    runtime = _Runtime(tmp_path)
    try:
        monkeypatch.setattr(runtime, "project_message", lambda **_: None)
        event_id = _append_message(runtime, chat_id=DIRECT, message_id="missing")
        report = asyncio.run(_catchup(runtime).run_once())
        assert report.still_unknown == {"missing_history_message": 1}
        runtime.store.revoke_event_source_authority(event_id, revision=1, now_ms=NOW)
        report = asyncio.run(_catchup(runtime).run_once())
        assert report.still_unknown == {"revoked": 1}
        assert report.registered == 0
    finally:
        runtime.close()


def test_catchup_lookback_pagination_and_revocation(tmp_path):
    runtime = _Runtime(tmp_path, history_members=(SENDER, OTHER))
    try:
        for message_id, occurred in (("old", NOW - 8 * 86_400_000),
                                     ("edge", NOW - 7 * 86_400_000), ("revoked", NOW)):
            _append_message(runtime, chat_id=GROUP, message_id=message_id, occurred_ms=occurred)
        source, _ = _source(runtime, GROUP, "revoked")
        runtime.knowledge.history_source_ledger.revoke(*source.key, reason="synthetic")
        report = asyncio.run(_catchup(runtime, batch_size=1).run_once())
        assert report.examined == 2
        assert report.already_proven == 1
        assert report.still_unknown == {"revoked": 1}
        assert _source(runtime, GROUP, "revoked")[0] is None
        all_report = asyncio.run(_catchup(runtime, lookback_days=None, batch_size=1).run_once())
        assert all_report.examined == 3
    finally:
        runtime.close()


@pytest.mark.asyncio
async def test_catchup_lifecycle_waits_for_ready_and_repeats(tmp_path, monkeypatch):
    runtime = _Runtime(tmp_path)
    catchup = _catchup(runtime, interval_seconds=0.01)
    status = "starting"
    calls = 0
    repeated = asyncio.Event()
    monkeypatch.setattr(runtime.history, "health", lambda: {"status": status})

    async def run_once():
        nonlocal calls
        calls += 1
        if calls == 2:
            repeated.set()

    monkeypatch.setattr(catchup, "run_once", run_once)
    try:
        await catchup.start()
        await asyncio.sleep(0)
        assert calls == 0
        status = "ready"
        await asyncio.wait_for(repeated.wait(), timeout=30)
        await catchup.stop()
        assert catchup._task is None
    finally:
        await catchup.stop()
        runtime.close()


def test_catchup_refuses_revocation_while_acquiring_lease(tmp_path, monkeypatch):
    runtime = _Runtime(tmp_path, history_members=(SENDER, OTHER))
    try:
        async def paused():
            raise HistoryPaused("starting")

        monkeypatch.setattr(runtime.history, "read_turn", paused)
        event_id = _append_message(runtime, chat_id=GROUP, message_id="race")

        async def revoke_then_read():
            runtime.store.revoke_event_source_authority(event_id, now_ms=NOW)
            return runtime.history.snapshot()

        monkeypatch.setattr(runtime.history, "read_turn", revoke_then_read)
        monkeypatch.setattr(runtime.registrar, "register", lambda _: pytest.fail("revoked"))
        report = asyncio.run(_catchup(runtime).run_once())
        assert report.still_unknown == {"revoked": 1}
        assert _source(runtime, GROUP, "race")[0] is None
    finally:
        runtime.close()


def test_catchup_paused_at_registrar_leaves_source_unknown(tmp_path, monkeypatch):
    runtime = _Runtime(tmp_path, history_members=(SENDER, OTHER))
    try:
        async def paused():
            raise HistoryPaused("starting")

        with monkeypatch.context() as patch:
            patch.setattr(runtime.history, "read_turn", paused)
            event_id = _append_message(runtime, chat_id=GROUP, message_id="paused-pass")

        def pause_registration(_):
            raise HistoryPaused("generation_invalidated")

        monkeypatch.setattr(runtime.registrar, "register", pause_registration)
        report = asyncio.run(_catchup(runtime).run_once())
        assert report.still_unknown == {"history_paused": 1}
        assert report.registered == 0
        assert runtime.store.get_event_source_authority(event_id)["audience_status"] == "unknown"
        assert _source(runtime, GROUP, "paused-pass")[0] is None
    finally:
        runtime.close()
