"""A message that reaches the Gateway while history is still starting is processed, not lost."""

from __future__ import annotations

import asyncio
from pathlib import Path

from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.channels.whatsapp import (
    WhatsAppChannel,
    _BridgeEventWork,
)
from yeoman_gateway.history.live import HistoryPaused, HistoryProjector
from yeoman_shared.config.schema import WhatsAppConfig

from tests.gateway.history_test_helpers import ProjectedHistory

CHAT = "chat@g.us"


class StartingHistory:
    """Synthetic projector that reports ``starting`` until the test releases it."""

    def __init__(self, ready: ProjectedHistory | None = None, *, paused_reads: int = 0):
        self.status = "starting"
        self.ready = asyncio.Event()
        self.inner = ready
        self.paused_reads = paused_reads

    def release(self) -> None:
        self.status = "ready"
        self.ready.set()

    def health(self) -> dict:
        return {"status": self.status}

    async def wait_until_ready(self) -> bool:
        await self.ready.wait()
        return True

    async def read_turn(self):
        if self.paused_reads:
            self.paused_reads -= 1
            raise HistoryPaused("retained_backlog")
        return await self.inner.read_turn()


def _work(event_id: str = "event-1") -> _BridgeEventWork:
    return _BridgeEventWork(
        {}, "message", {}, event_id, (), asyncio.get_running_loop().create_future()
    )


def test_message_ack_waits_for_history_then_projects() -> None:
    channel = WhatsAppChannel(WhatsAppConfig(), MessageBus())
    order: list[str] = []

    async def ack(command_type, payload, timeout_seconds, **kwargs):
        order.append(command_type)
        return {"acknowledged": True}

    async def project(work):
        order.append("project")

    channel._send_command = ack  # type: ignore[method-assign]
    channel._project_bridge_event = project  # type: ignore[method-assign]

    async def run() -> None:
        history = StartingHistory()
        channel._history_projector = history
        work = _work()
        task = asyncio.create_task(channel._ack_and_project_bridge_work(work))
        await asyncio.sleep(0.05)
        assert order == []  # the Bridge still owns the message while history starts
        history.release()
        await asyncio.wait_for(task, timeout=5)
        assert order == ["ack_event", "project"]
        assert work.completion.result() is True

    asyncio.run(run())


def test_intake_close_while_history_starts_leaves_message_for_replay() -> None:
    channel = WhatsAppChannel(WhatsAppConfig(), MessageBus())
    acks: list[str] = []

    async def ack(command_type, payload, timeout_seconds, **kwargs):
        acks.append(command_type)
        return {"acknowledged": True}

    channel._send_command = ack  # type: ignore[method-assign]

    async def run() -> None:
        channel._history_projector = StartingHistory()
        work = _work()
        task = asyncio.create_task(channel._ack_and_project_bridge_work(work))
        await asyncio.sleep(0.05)
        channel._stopping = True
        await asyncio.wait_for(task, timeout=5)
        assert acks == []
        assert work.completion.result() is False

    asyncio.run(run())


def test_non_message_frames_are_not_held() -> None:
    channel = WhatsAppChannel(WhatsAppConfig(), MessageBus())
    acks: list[str] = []

    async def ack(command_type, payload, timeout_seconds, **kwargs):
        acks.append(command_type)
        return {"acknowledged": True}

    channel._send_command = ack  # type: ignore[method-assign]

    async def run() -> None:
        channel._history_projector = StartingHistory()
        work = _BridgeEventWork(
            {}, "reaction", {}, "reaction-1", (), asyncio.get_running_loop().create_future()
        )
        await asyncio.wait_for(channel._ack_and_project_bridge_work(work), timeout=5)
        assert acks == ["ack_event"]

    asyncio.run(run())


def test_ingest_retries_after_history_pause(tmp_path: Path) -> None:
    channel = WhatsAppChannel(WhatsAppConfig(debounce_ms=0), MessageBus())
    channel._chat_registry = None
    published: list[str] = []

    async def publish(event):
        published.append(event.message_id)

    channel._publish_event = publish  # type: ignore[method-assign]

    async def run() -> None:
        history = StartingHistory(ProjectedHistory(tmp_path / "history"), paused_reads=1)
        history.release()
        channel._history_projector = history
        event = channel._parse_inbound_event(
            {"chatJid": CHAT, "messageId": "m-1", "senderId": "4915", "text": "hallo"}
        )
        assert event is not None
        await asyncio.wait_for(channel._ingest_inbound_event(event), timeout=5)

    asyncio.run(run())
    assert published == ["m-1"]


def test_projector_wait_until_ready_follows_status() -> None:
    projector = object.__new__(HistoryProjector)
    projector._stopping = False

    async def run() -> None:
        projector._status = "starting"
        waiter = asyncio.create_task(projector.wait_until_ready(poll_seconds=0.01))
        await asyncio.sleep(0.05)
        assert not waiter.done()
        projector._status = "ready"
        assert await asyncio.wait_for(waiter, timeout=1) is True

        projector._status = "failed"
        assert await projector.wait_until_ready(poll_seconds=0.01) is False

        projector._status = "rebuilding"
        projector._stopping = True
        assert await projector.wait_until_ready(poll_seconds=0.01) is False

    asyncio.run(run())

