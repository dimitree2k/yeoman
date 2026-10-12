"""Synthetic retired-mode capture and intent isolation witnesses."""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from yeoman_gateway.app.bootstrap import OrchestratorService
from yeoman_gateway.core.intents import (
    SendOutboundIntent,
)
from yeoman_gateway.core.models import InboundEvent, OutboundEvent, PolicyDecision
from yeoman_gateway.core.orchestrator import Orchestrator
from yeoman_shared.telemetry import InMemoryTelemetry

from tests.gateway.history_test_helpers import ProjectedHistory


@asynccontextmanager
async def _history_scope(tmp_path):
    history = ProjectedHistory(tmp_path / "history-dispatch")
    try:
        async with history.turn():
            yield
    finally:
        history.close()


def service(bus):
    return OrchestratorService(bus=bus, orchestrator=None, typing_adapter=AsyncMock(),
                               telemetry=InMemoryTelemetry())


@pytest.mark.asyncio
async def test_failed_intent_does_not_abort_remaining_intents():
    bus = AsyncMock()
    bus.publish_outbound.side_effect = [RuntimeError('synthetic failure'), None]
    await service(bus)._dispatch_intents([SendOutboundIntent(event=OutboundEvent(
        channel='telegram', chat_id='synthetic', content=text)) for text in ('first', 'second')])
    assert bus.publish_outbound.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['[idea] synthetic', 'backlog: synthetic'])
async def test_former_idea_command_is_an_ordinary_message(text, tmp_path):
    class Policy:
        def evaluate(self, event):
            return PolicyDecision(accept_message=True, should_respond=False,
                allowed_tools=frozenset(), reason='synthetic')
    orchestrator = Orchestrator(policy=Policy(), responder=AsyncMock(),
        reply_context_window_limit=0, reply_context_line_max_chars=100)
    history = ProjectedHistory(tmp_path / "history-postcutover-idea")
    async with history.turn():
        intents = await orchestrator.handle(InboundEvent(channel='whatsapp', chat_id='synthetic',
            sender_id='10001', content=text, message_id='m'))
    history.close()
    assert not any(isinstance(i, SendOutboundIntent) for i in intents)
