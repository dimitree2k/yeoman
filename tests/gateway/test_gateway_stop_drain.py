"""A gateway stop lets running replies finish before the channels close."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from yeoman_gateway.app.bootstrap import GatewayRuntime, OrchestratorService
from yeoman_gateway.channels.manager import ChannelManager


def _service() -> OrchestratorService:
    return OrchestratorService.__new__(OrchestratorService)


def test_wait_idle_waits_for_running_messages_without_cancelling() -> None:
    async def scenario() -> tuple[int, bool]:
        service = _service()
        reply = asyncio.ensure_future(asyncio.sleep(0.05))
        service._tasks = {reply}
        reply.add_done_callback(service._tasks.discard)
        left = await service.wait_idle(timeout=5)
        return left, reply.cancelled()

    assert asyncio.run(scenario()) == (0, False)


def test_wait_idle_gives_up_at_the_timeout_and_leaves_the_task_running() -> None:
    async def scenario() -> tuple[int, bool]:
        service = _service()
        stuck = asyncio.ensure_future(asyncio.Event().wait())
        service._tasks = {stuck}
        left = await service.wait_idle(timeout=0.05)
        running = not stuck.done()
        stuck.cancel()
        return left, running

    assert asyncio.run(scenario()) == (1, True)


class _Orchestrator:
    def __init__(self, in_flight: int) -> None:
        self.in_flight = in_flight
        self.events: list[str] = []

    async def wait_idle(self, *, timeout: float) -> int:
        self.events.append(f"wait {timeout}")
        return 0


def test_prepare_stop_pauses_intake_then_waits_for_replies() -> None:
    orchestrator = _Orchestrator(in_flight=1)
    channels = SimpleNamespace(pause_intake=lambda: orchestrator.events.append("pause"))
    runtime = SimpleNamespace(orchestrator=orchestrator, channels=channels)

    asyncio.run(GatewayRuntime.prepare_stop(runtime, timeout=7))  # type: ignore[arg-type]

    assert orchestrator.events == ["pause", "wait 7"]


def test_prepare_stop_with_nothing_running_only_pauses_intake() -> None:
    orchestrator = _Orchestrator(in_flight=0)
    channels = SimpleNamespace(pause_intake=lambda: orchestrator.events.append("pause"))
    runtime = SimpleNamespace(orchestrator=orchestrator, channels=channels)

    asyncio.run(GatewayRuntime.prepare_stop(runtime))  # type: ignore[arg-type]

    assert orchestrator.events == ["pause"]


def test_channel_manager_pauses_channels_that_support_it() -> None:
    paused: list[str] = []

    class _Pausable:
        def pause_intake(self) -> None:
            paused.append("wa")

    class _Broken:
        def pause_intake(self) -> None:
            raise RuntimeError("boom")

    manager = ChannelManager.__new__(ChannelManager)
    manager.channels = {"whatsapp": _Pausable(), "telegram": object(), "other": _Broken()}

    manager.pause_intake()

    assert paused == ["wa"]
