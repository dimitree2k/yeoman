"""Tests for ContactsMiddleware.

The middleware is a *bridge*: a channel adapter proves a platform mapping, the knowledge
facade decides the person.  These tests cover the bridge itself - which identifiers are
handed over, which are refused, and what happens when knowledge is degraded.  All values
are synthetic.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from yeoman_gateway.core.models import InboundEvent
from yeoman_gateway.core.pipeline import PipelineContext
from yeoman_gateway.pipeline.contacts import ContactsMiddleware, event_timestamp_ms

OBSERVED_AT_S = 1_700_000_000
OBSERVED_AT_MS = OBSERVED_AT_S * 1000


def _make_event(**overrides: object) -> InboundEvent:
    defaults = {
        "channel": "whatsapp",
        "chat_id": "test-chat@g.us",
        "sender_id": "491521234567",
        "content": "hello",
        "message_id": "msg-001",
        "raw_metadata": {
            "message_id": "msg-001",
            "sender_name": "Alex",
            "account_id": "account-synthetic-1",
            "sender_phone_jid": "491521234567@s.whatsapp.net",
            "timestamp": OBSERVED_AT_MS,
        },
    }
    defaults.update(overrides)
    return InboundEvent(**defaults)


@pytest.fixture
def knowledge(tmp_path: Path):
    """A synthetic knowledge facade on a temporary database."""
    from yeoman_gateway.knowledge.api import open_knowledge_store
    from yeoman_gateway.knowledge.authority import (
        EvidenceAudience,
        FakeClock,
        FakePolicyAuthority,
        FakeSourceAuthority,
    )

    authority = FakeSourceAuthority()
    clock = FakeClock()
    service = open_knowledge_store(
        tmp_path / "knowledge.db",
        workspace_id="contacts-middleware-tests",
        source_authority=authority,
        policy_authority=FakePolicyAuthority(
            admins={"whatsapp:4910000000001"}, capture_actors={"whatsapp:4910000000001"}
        ),
        clock=clock,
    )
    del EvidenceAudience  # imported for parity with the other suites; unused here

    class _IssuingService:
        """The facade plus a way to issue the evidence a channel adapter would prove."""

        def __init__(self, inner, authority) -> None:
            self._inner = inner
            self._authority = authority

        def issue_for(self, event: InboundEvent) -> None:
            """Register exactly the observation the middleware will build for ``event``."""
            observation = ContactsMiddleware(knowledge=None)._observation(  # noqa: SLF001
                event.channel, event.participant, event.sender_id, dict(event.raw_metadata)
            )
            if observation is None:
                # Nothing the adapter could prove (for example a bare number): the
                # middleware will not resolve anything either.
                return
            self._authority.issue_observation(observation)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    wrapper = _IssuingService(service, authority)
    try:
        yield wrapper
    finally:
        service.close()


async def _run(
    middleware: ContactsMiddleware, event: InboundEvent, *, issue: bool = True
) -> PipelineContext:
    if issue and middleware._knowledge is not None:  # noqa: SLF001 - adapter evidence seam
        issuer = getattr(middleware._knowledge, "issue_for", None)  # noqa: SLF001
        if callable(issuer):
            issuer(event)
    ctx = PipelineContext(event=event)
    called = False

    async def next_fn(c: PipelineContext) -> None:
        nonlocal called
        called = True

    if event.channel == "whatsapp":
        from yeoman_gateway.history.context import history_turn

        from tests.gateway.history_turn_fixtures import Projector

        projector = Projector()
        try:
            async with history_turn(projector):
                await middleware(ctx, next_fn)
        finally:
            projector.close()
    else:
        await middleware(ctx, next_fn)
    assert called, "the middleware must always continue the pipeline"
    return ctx




def test_event_timestamp_is_normalized_without_guessing() -> None:
    assert event_timestamp_ms({"timestamp": OBSERVED_AT_MS}) == OBSERVED_AT_MS
    assert event_timestamp_ms({"timestamp": OBSERVED_AT_S}) == OBSERVED_AT_MS
    assert event_timestamp_ms({"timestamp": datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)}) == (
        OBSERVED_AT_MS
    )
    assert event_timestamp_ms({}) == 0
    assert event_timestamp_ms({"timestamp": True}) == 0


