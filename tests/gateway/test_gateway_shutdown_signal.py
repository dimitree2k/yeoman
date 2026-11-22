"""SIGTERM must run the runtime's cleanup instead of ending the process at once."""

import asyncio
import os
import signal

import pytest
from yeoman_gateway.cli.gateway_commands import _run_until_stopped


def test_sigterm_cancels_main_and_runs_its_cleanup():
    events: list[str] = []

    async def main() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)  # cleanup may still await after the cancellation
            events.append("cleaned")

    async def scenario() -> None:
        asyncio.get_running_loop().call_later(0.05, os.kill, os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(_run_until_stopped(main()), timeout=30)

    asyncio.run(scenario())
    assert events == ["cleaned"]
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL


def test_a_cancellation_not_caused_by_a_signal_still_propagates():
    async def scenario() -> None:
        task = asyncio.ensure_future(_run_until_stopped(asyncio.Event().wait()))
        await asyncio.sleep(0.01)
        task.cancel()
        await task

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scenario())


def test_sigterm_waits_for_prepare_stop_before_cancelling_main():
    events: list[str] = []

    async def main() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            events.append("cancelled")

    async def prepare_stop() -> None:
        events.append("drain started")
        await asyncio.sleep(0.05)  # a reply still being written and sent
        events.append("drain done")

    async def scenario() -> None:
        asyncio.get_running_loop().call_later(0.05, os.kill, os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(_run_until_stopped(main(), prepare_stop), timeout=30)

    asyncio.run(scenario())
    assert events == ["drain started", "drain done", "cancelled"]


def test_second_signal_skips_the_drain():
    events: list[str] = []

    async def main() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            events.append("cancelled")

    async def prepare_stop() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            events.append("drain cut short")
            raise

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        loop.call_later(0.05, os.kill, os.getpid(), signal.SIGTERM)
        loop.call_later(0.15, os.kill, os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(_run_until_stopped(main(), prepare_stop), timeout=30)

    asyncio.run(scenario())
    assert events == ["drain cut short", "cancelled"]


def test_a_failing_drain_still_stops():
    events: list[str] = []

    async def main() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            events.append("cancelled")

    async def prepare_stop() -> None:
        raise RuntimeError("boom")

    async def scenario() -> None:
        asyncio.get_running_loop().call_later(0.05, os.kill, os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(_run_until_stopped(main(), prepare_stop), timeout=30)

    asyncio.run(scenario())
    assert events == ["cancelled"]
