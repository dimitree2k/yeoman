"""WhatsApp sender identity is projected from native history evidence."""

from datetime import UTC, datetime

import pytest
from yeoman_gateway.core.models import InboundEvent
from yeoman_gateway.core.pipeline import PipelineContext
from yeoman_gateway.pipeline.contacts import ContactsMiddleware

from tests.gateway.history_test_helpers import ProjectedHistory

OBSERVED_AT_S = 1_700_000_000
OBSERVED_AT_MS = OBSERVED_AT_S * 1000
CHAT = "test-chat@g.us"
PHONE = "491521234567@s.whatsapp.net"


def _event(*, sender_id: str = PHONE, **metadata: object) -> InboundEvent:
    raw: dict[str, object] = {
        "message_id": "msg-001",
        "sender_phone_jid": PHONE,
        "account_id": "account-synthetic-1",
        "timestamp": OBSERVED_AT_MS,
    }
    raw.update(metadata)
    return InboundEvent(
        channel="whatsapp",
        chat_id=CHAT,
        sender_id=sender_id,
        content="hello",
        message_id="msg-001",
        timestamp=datetime.fromtimestamp(OBSERVED_AT_S, UTC),
        raw_metadata=raw,
    )


async def _run(middleware: ContactsMiddleware, event: InboundEvent) -> PipelineContext:
    ctx = PipelineContext(event=event)
    called = False

    async def next_fn(_ctx: PipelineContext) -> None:
        nonlocal called
        called = True

    await middleware(ctx, next_fn)
    assert called
    return ctx


@pytest.mark.asyncio
async def test_sender_resolves_from_projected_native_identity(tmp_path) -> None:
    projected = ProjectedHistory(
        tmp_path / "history",
        contacts=[dict(identifiers=[PHONE], name="Alex", timestamp=OBSERVED_AT_MS)],
        messages=[dict(
            channel="whatsapp", chat_id=CHAT, message_id="msg-001", text="hello",
            timestamp=OBSERVED_AT_S, sender_id=PHONE,
        )],
    )
    try:
        async with projected.turn():
            ctx = await _run(ContactsMiddleware(), _event())
        assert ctx.event.raw_metadata["contact_id"]
        assert ctx.event.raw_metadata["identity_status"] == "resolved"
        assert ctx.event.raw_metadata["identity_reason"] == "history"
    finally:
        projected.close()


@pytest.mark.asyncio
async def test_unresolved_sender_does_not_keep_stale_identity_metadata(tmp_path) -> None:
    projected = ProjectedHistory(tmp_path / "empty-history")
    try:
        async with projected.turn():
            ctx = await _run(
                ContactsMiddleware(),
                _event(
                    sender_id="4915000000000@s.whatsapp.net",
                    contact_id="stale-person",
                    identity_status="resolved",
                    identity_reason="legacy",
                ),
            )
        assert "contact_id" not in ctx.event.raw_metadata
        assert "identity_status" not in ctx.event.raw_metadata
        assert "identity_reason" not in ctx.event.raw_metadata
    finally:
        projected.close()

