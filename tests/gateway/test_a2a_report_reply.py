from __future__ import annotations

from yeoman_gateway.core.models import InboundEvent, PolicyDecision
from yeoman_gateway.core.pipeline import PipelineContext
from yeoman_gateway.pipeline.responder import ResponderMiddleware


async def test_owner_quoted_langfassung_uses_stored_report_without_llm() -> None:
    class Responder:
        async def generate_reply(self, *args):
            raise AssertionError("must not start a model or A2A run")

    event = InboundEvent(
        channel="whatsapp", chat_id="first@g.us", sender_id="owner",
        content="Langfassung bitte.", reply_to_bot=True, reply_to_message_id="card-1",
    )
    ctx = PipelineContext(event=event, decision=PolicyDecision(
        accept_message=True, should_respond=True, allowed_tools=frozenset(),
        reason="owner", is_owner=True,
    ))
    middleware = ResponderMiddleware(
        responder=Responder(), report_lookup=lambda event: "Full report with risks",
    )

    async def next_layer(ctx):
        assert ctx.reply == "Full report with risks"

    await middleware(ctx, next_layer)


async def test_missing_old_report_explains_without_new_run() -> None:
    class Responder:
        async def generate_reply(self, *args):
            raise AssertionError("must not start a model or A2A run")

    event = InboundEvent(
        channel="whatsapp", chat_id="first@g.us", sender_id="owner",
        content="Vollbericht bitte", reply_to_bot=True, reply_to_message_id="old-card",
    )
    ctx = PipelineContext(event=event, decision=PolicyDecision(
        accept_message=True, should_respond=True, allowed_tools=frozenset(),
        reason="owner", is_owner=True,
    ))
    middleware = ResponderMiddleware(responder=Responder(), report_lookup=lambda event: "")

    async def next_layer(ctx):
        assert "nicht gespeichert" in ctx.reply

    await middleware(ctx, next_layer)


async def test_cached_stock_request_in_another_chat_skips_llm() -> None:
    class Responder:
        async def generate_reply(self, *args):
            raise AssertionError("must not start a model or A2A run")

    event = InboundEvent(
        channel="whatsapp", chat_id="other@g.us", sender_id="owner",
        content="Bitte Apple (AAPL) als Aktie kurz einschätzen.",
    )
    ctx = PipelineContext(event=event, decision=PolicyDecision(
        accept_message=True, should_respond=True, allowed_tools=frozenset(),
        reason="owner", is_owner=True,
    ))
    middleware = ResponderMiddleware(responder=Responder(), report_lookup=lambda event: "Cached Hold card")

    async def next_layer(ctx):
        assert ctx.reply == "Cached Hold card"

    await middleware(ctx, next_layer)
