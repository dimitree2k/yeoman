from __future__ import annotations

import asyncio
from typing import Any

import pytest
from yeoman_gateway.agent.tools.delete_message import DeleteMessageTool
from yeoman_gateway.bus.events import OutboundMessage


@pytest.mark.asyncio
async def test_delete_message_uses_reply_target_in_current_whatsapp_chat() -> None:
    outbound: list[OutboundMessage] = []

    async def publish(message: OutboundMessage) -> None:
        outbound.append(message)

    tool = DeleteMessageTool(send_callback=publish)
    tool.set_context(
        "whatsapp",
        "12345@g.us",
        reply_to_message_id="BAE5QUOTEDMESSAGE",
        is_owner=True,
    )

    result = await tool.execute()

    assert result == (
        "Delete request queued for whatsapp:12345@g.us; "
        "message_id=BAE5QUOTEDMESSAGE. Only Arvid's own WhatsApp messages can be deleted."
    )
    assert outbound == [
        OutboundMessage(
            channel="whatsapp",
            chat_id="12345@g.us",
            content="",
            metadata={"delete_message": {"message_id": "BAE5QUOTEDMESSAGE"}},
        )
    ]


@pytest.mark.asyncio
async def test_delete_message_accepts_explicit_target_and_resolves_group() -> None:
    outbound: list[OutboundMessage] = []

    async def publish(message: OutboundMessage) -> None:
        outbound.append(message)

    tool = DeleteMessageTool(
        send_callback=publish,
        group_resolver=lambda reference: (
            "67890@g.us" if reference == "team" else None,
            None if reference == "team" else "group not found",
        ),
    )
    tool.set_context(
        "whatsapp",
        "owner@s.whatsapp.net",
        is_owner=True,
    )

    result = await tool.execute(message_id="BAE5EXPLICIT", group="team")

    assert "whatsapp:67890@g.us" in result
    assert outbound[0].chat_id == "67890@g.us"
    assert outbound[0].metadata == {"delete_message": {"message_id": "BAE5EXPLICIT"}}


@pytest.mark.asyncio
async def test_delete_message_rejects_non_whatsapp_and_missing_target() -> None:
    calls: list[Any] = []

    async def publish(message: OutboundMessage) -> None:
        calls.append(message)

    tool = DeleteMessageTool(send_callback=publish)
    tool.set_context("telegram", "chat-1", is_owner=True)

    assert await tool.execute(message_id="message-1") == (
        "Error: delete_message only supports WhatsApp"
    )

    tool.set_context("whatsapp", "12345@s.whatsapp.net", is_owner=True)
    assert await tool.execute() == (
        "Error: No message_id specified; quote/reply to the message or provide its exact ID"
    )
    assert calls == []

    tool.set_context("whatsapp", "12345@s.whatsapp.net")
    assert await tool.execute(message_id="message-1") == (
        "Error: Message deletion is restricted to the owner"
    )
    assert calls == []


@pytest.mark.asyncio
async def test_delete_message_does_not_mix_reply_target_with_another_chat() -> None:
    calls: list[OutboundMessage] = []

    async def publish(message: OutboundMessage) -> None:
        calls.append(message)

    tool = DeleteMessageTool(send_callback=publish)
    tool.set_context(
        "whatsapp",
        "12345@g.us",
        reply_to_message_id="BAE5CURRENTCHAT",
        is_owner=True,
    )

    result = await tool.execute(chat_id="67890@g.us")

    assert result == (
        "Error: An explicit message_id is required when targeting a different chat"
    )
    assert calls == []


@pytest.mark.asyncio
async def test_delete_message_context_is_isolated_between_concurrent_turns() -> None:
    outbound: list[OutboundMessage] = []

    async def publish(message: OutboundMessage) -> None:
        outbound.append(message)

    tool = DeleteMessageTool(send_callback=publish)

    async def run_turn(
        *,
        chat_id: str,
        reply_to_message_id: str,
        is_owner: bool,
    ) -> str:
        tool.set_context(
            "whatsapp",
            chat_id,
            reply_to_message_id=reply_to_message_id,
            is_owner=is_owner,
        )
        await asyncio.sleep(0)
        return await tool.execute()

    owner_result, non_owner_result = await asyncio.gather(
        run_turn(
            chat_id="12345@s.whatsapp.net",
            reply_to_message_id="BAE5OWNER",
            is_owner=True,
        ),
        run_turn(
            chat_id="67890@s.whatsapp.net",
            reply_to_message_id="BAE5OTHER",
            is_owner=False,
        ),
    )

    assert "message_id=BAE5OWNER" in owner_result
    assert non_owner_result == "Error: Message deletion is restricted to the owner"
    assert [message.chat_id for message in outbound] == ["12345@s.whatsapp.net"]


@pytest.mark.asyncio
async def test_responder_context_passes_reply_target_to_delete_tool(tmp_path) -> None:
    from yeoman_gateway.adapters.responder_llm import LLMResponder
    from yeoman_gateway.bus.queue import MessageBus
    from yeoman_gateway.providers.base import LLMProvider

    class Provider(LLMProvider):
        async def chat(self, *args: Any, **kwargs: Any):
            raise AssertionError("chat should not be called")

        def get_default_model(self) -> str:
            return "dummy/model"

    responder = LLMResponder(
        bus=MessageBus(),
        provider=Provider(),
        workspace=tmp_path,
        max_iterations=1,
    )
    responder._set_tool_context(
        channel="whatsapp",
        chat_id="12345@s.whatsapp.net",
        session_key="whatsapp:12345@s.whatsapp.net",
        reply_to_message_id="BAE5REPLYTARGET",
        is_owner=True,
    )

    tool = responder.tools.get("delete_message")
    assert isinstance(tool, DeleteMessageTool)
    assert await tool.execute() == (
        "Delete request queued for whatsapp:12345@s.whatsapp.net; "
        "message_id=BAE5REPLYTARGET. Only Arvid's own WhatsApp messages can be deleted."
    )
