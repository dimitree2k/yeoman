"""Plan 06 / Aufgabe 3: cutover and the way back are configuration, not data surgery."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from yeoman_gateway.adapters.policy_engine import EnginePolicyAdapter
from yeoman_gateway.app.bootstrap import (
    build_effect_router,
    build_processing_gate,
    build_processing_store,
    build_thread_registry,
)
from yeoman_gateway.bus.events import OutboundMessage
from yeoman_gateway.core.models import InboundEvent
from yeoman_gateway.policy.engine import PolicyEngine
from yeoman_gateway.policy.loader import save_policy
from yeoman_gateway.policy.schema import PolicyConfig
from yeoman_gateway.processing.dispatch import (
    EFFECT_PROVENANCE_KEY,
    ManagedOutboundDispatcher,
    managed_outbound_guard,
)
from yeoman_gateway.processing.policy import IngestRequest
from yeoman_shared.config.schema import Config

CHAT = "pilot@g.us"
T0 = 1_700_000_000_000


@dataclass
class _Bus:
    """Records legacy publishes instead of delivering them."""

    published: list[str] = field(default_factory=list)

    async def publish_outbound(self, message: OutboundMessage) -> None:
        self.published.append(message.content)


@dataclass
class _Transport:
    sent: list[str] = field(default_factory=list)

    async def send_now(self, message) -> None:
        self.sent.append(message.content)

    async def send_reaction_now(self, message) -> None:  # pragma: no cover - unused
        self.sent.append(message.emoji)


@dataclass
class _Runtime:
    store: object
    registry: object
    gate: object
    router: object
    bus: _Bus
    transport: _Transport
    config: Config


def _runtime(
    tmp_path: Path, *, enabled: bool = True, chats: tuple[str, ...] = (), shadow: tuple[str, ...] = ()
) -> _Runtime:
    policy_path = tmp_path / "policy.json"
    save_policy(
        PolicyConfig.model_validate(
            {
                "owners": {"whatsapp": ["owner@s.whatsapp.net"]},
                "channels": {
                    "whatsapp": {
                        "default": {
                            "whoCanTalk": {"mode": "everyone"},
                            "whenToReply": {"mode": "all"},
                        }
                    }
                },
            }
        ),
        policy_path,
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    adapter = EnginePolicyAdapter(
        engine=PolicyEngine(
            PolicyConfig.model_validate(
                {"owners": {"whatsapp": ["owner@s.whatsapp.net"]}}
            ),
            workspace=workspace,
            apply_channels={"whatsapp"},
        ),
        known_tools={"message"},
        policy_path=policy_path,
        workspace=workspace,
    )
    config = Config.model_validate(
        {
            "processing": {
                "enabled": enabled,
                "chats": [f"whatsapp:{chat}" for chat in chats],
                "shadow_chats": [f"whatsapp:{chat}" for chat in shadow],
                "db_path": str(tmp_path / "processing.db"),
            },
            "security": {"enabled": False},
        }
    )
    bus = _Bus()
    with patch.dict(os.environ, {"YEOMAN_HOME": str(tmp_path)}):
        store = build_processing_store(config)
        assert store is not None
        registry = build_thread_registry(config, store)
        gate = build_processing_gate(config, adapter, store, registry)
        router = build_effect_router(config, adapter, store, bus, threads=registry)
    assert registry is not None and gate is not None and router is not None
    transport = _Transport()
    router.set_direct_transport(transport.send_now, transport.send_reaction_now)
    return _Runtime(
        store=store, registry=registry, gate=gate, router=router, bus=bus,
        transport=transport, config=config,
    )


def _event(*, message_id: str = "m1", chat: str = CHAT) -> InboundEvent:
    return InboundEvent(
        channel="whatsapp",
        chat_id=chat,
        sender_id="orderer@s.whatsapp.net",
        content="hi",
        message_id=message_id,
        is_group=True,
        mentioned_bot=True,
        timestamp=datetime(2023, 11, 14, tzinfo=UTC),
    )


def _admit(runtime: _Runtime, *, message_id: str = "m1", chat: str = CHAT):
    return runtime.gate.admit(
        IngestRequest(
            event_key=f"whatsapp:{chat}:{message_id}",
            event_id=message_id,
            trace_id=f"tr-{message_id}",
            event=_event(message_id=message_id, chat=chat),
        )
    )


@pytest.mark.asyncio
async def test_shadow_chat_decides_without_sending_or_storing_effects(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, shadow=(CHAT,))
    try:
        result = _admit(runtime)
        assert result.shadow is True
        assert result.proceed is True  # the user-visible path is unchanged

        dispatcher = ManagedOutboundDispatcher(router=runtime.router, bus=runtime.bus)
        await dispatcher(
            OutboundMessage(channel="whatsapp", chat_id=CHAT, content="answer")
        )

        assert runtime.store.list_effects() == ()
        assert runtime.transport.sent == []
        assert runtime.bus.published == ["answer"]  # legacy delivery, untouched
    finally:
        runtime.store.close()


@pytest.mark.asyncio
async def test_deactivated_chat_keeps_the_legacy_path(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, chats=())
    try:
        guard = managed_outbound_guard(runtime.router)
        allowed, reason = guard(OutboundMessage(channel="whatsapp", chat_id=CHAT, content="x"))
        assert (allowed, reason) == (True, "unmanaged")

        dispatcher = ManagedOutboundDispatcher(router=runtime.router, bus=runtime.bus)
        await dispatcher(OutboundMessage(channel="whatsapp", chat_id=CHAT, content="legacy"))

        assert runtime.store.count_send_budget_reservations() == 0
        assert runtime.store.list_effects() == ()
        assert runtime.bus.published == ["legacy"]
    finally:
        runtime.store.close()


def test_activated_chat_refuses_legacy_outbound_without_provenance(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, chats=(CHAT,))
    try:
        guard = managed_outbound_guard(runtime.router)

        bare = guard(OutboundMessage(channel="whatsapp", chat_id=CHAT, content="x"))
        forged = guard(
            OutboundMessage(
                channel="whatsapp",
                chat_id=CHAT,
                content="x",
                metadata={EFFECT_PROVENANCE_KEY: "fx-does-not-exist"},
            )
        )

        assert bare == (False, "legacy_outbound_without_effect")
        assert forged == (False, "forged_effect_provenance")
    finally:
        runtime.store.close()


def test_rollback_cancels_queued_work_and_never_retries_the_unknown(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, chats=(CHAT,))
    store = runtime.store
    try:
        _admit(runtime)
        turn_id = str(store.list_threads()[0].thread_id)
        turn = store.active_turn(turn_id)
        store.enqueue_effect(
            effect_id="fx-queued",
            operation_key="rollback:queued",
            payload={"text": "never sent"},
            target={"channel": "whatsapp", "chat_id": CHAT},
            turn_id=turn.turn_id,
            turn_revision=turn.revision,
            now_ms=T0,
        )
        store.enqueue_effect(
            effect_id="fx-unknown",
            operation_key="rollback:unknown",
            payload={"text": "outcome unproven"},
            target={"channel": "whatsapp", "chat_id": CHAT},
            turn_id=turn.turn_id,
            turn_revision=turn.revision,
            now_ms=T0,
        )
        assert store.transition(
            "fx-unknown",
            expected="queued",
            target="executing",
            now_ms=T0 + 1,
            worker_id="test-worker",
        )
        assert store.transition(
            "fx-unknown",
            expected="executing",
            target="unknown",
            now_ms=T0 + 2,
            worker_id="test-worker",
        )

        # The way back: stop new admissions, cancel what never ran, keep what is unproven.
        runtime.config.processing.enabled = False
        assert store.transition(
            "fx-queued",
            expected="queued",
            target="cancelled",
            now_ms=T0 + 3,
            evidence={"reason": "rollback"},
        )

        assert store.effect_state("fx-queued") == "cancelled"
        assert store.effect_state("fx-unknown") == "unknown"
        # Recovery after a restart never promotes an unproven outcome into a retry.
        assert store.recover_executing(now_ms=T0 + 4) == ()
        assert store.effect_state("fx-unknown") == "unknown"
        assert runtime.transport.sent == []
    finally:
        runtime.store.close()


def test_rollback_deletes_no_database_and_no_archive(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, chats=(CHAT,))
    store = runtime.store
    _admit(runtime)
    before = len(store.list_threads())
    db_path = Path(runtime.config.processing.db_path)
    assert db_path.exists()
    store.close()

    runtime.config.processing.enabled = False
    assert build_processing_store(runtime.config) is None  # new mode offline

    # The data is still there: rollback is a switch, not a migration back.
    import sqlite3

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        assert conn.execute("select count(*) from threads").fetchone()[0] == before
        assert conn.execute("select value from meta where key='schema_version'").fetchone()[0]
    finally:
        conn.close()
