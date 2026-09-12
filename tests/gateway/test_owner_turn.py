from __future__ import annotations

from types import SimpleNamespace

import pytest
from yeoman_gateway.ipc.owner_turn import process_a2a_delivery, process_owner_turn


class _FakePolicy:
    def __init__(self, *, owner: bool = True, should_respond: bool = True) -> None:
        self.owner = owner
        self.should_respond = should_respond
        self.events = []

    def owner_recipients(self, channel: str) -> list[str]:
        assert channel == "whatsapp"
        return ["owner@lid"]

    def evaluate(self, event):
        self.events.append(event)
        return SimpleNamespace(
            is_owner=self.owner,
            accept_message=True,
            should_respond=self.should_respond,
            reason="paused" if not self.should_respond else "owner",
            allowed_tools=frozenset({"memory_search"}),
            persona_text="Arvid persona",
            model_profile="owner-profile",
        )


class _FakeResponder:
    def __init__(self) -> None:
        self.calls = []

    async def process_direct(self, content: str, **kwargs):
        self.calls.append((content, kwargs))
        return "Yeoman answer"


class _FakeBus:
    def __init__(self) -> None:
        self.messages = []

    async def publish_outbound(self, message) -> None:
        self.messages.append(message)


class _FakeA2APolicy:
    def __init__(self, *, allowed_tools: frozenset[str] | None = None) -> None:
        self.events = []
        self.allowed_tools = (
            frozenset({"send_voice"}) if allowed_tools is None else allowed_tools
        )

    def owner_recipients(self, channel: str) -> list[str]:
        assert channel == "whatsapp"
        return ["owner@lid"]

    def resolve_whatsapp_group(self, reference: str) -> tuple[str | None, str | None]:
        if reference == "Molty Python":
            return "molty@g.us", None
        return None, "unknown group reference"

    def evaluate(self, event):
        self.events.append(event)
        return SimpleNamespace(
            accept_message=True,
            should_respond=True,
            allowed_tools=self.allowed_tools,
            is_owner=False,
            reason="a2a allowed",
        )


class _FakeDeliveryResponder:
    def __init__(self) -> None:
        self.calls = []

    async def execute_delivery(self, **kwargs):
        self.calls.append(kwargs)
        return "Voice message delivered to whatsapp:molty@g.us."


@pytest.mark.asyncio
async def test_owner_turn_uses_canonical_session_and_policy_context() -> None:
    policy = _FakePolicy()
    responder = _FakeResponder()
    bus = _FakeBus()

    result = await process_owner_turn(
        prompt="Which memory facts do we have?",
        chat_id="owner@lid",
        session_key=None,
        post_to_whatsapp=False,
        policy_adapter=policy,
        responder=responder,
        bus=bus,
    )

    assert result == {
        "response": "Yeoman answer",
        "session_key": "whatsapp:owner@lid",
        "posted_to_whatsapp": False,
    }
    assert len(policy.events) == 1
    assert policy.events[0].channel == "whatsapp"
    assert policy.events[0].chat_id == "owner@lid"
    assert responder.calls == [
        (
            "Which memory facts do we have?",
            {
                "session_key": "whatsapp:owner@lid",
                "channel": "whatsapp",
                "chat_id": "owner@lid",
                "allowed_tools": {"memory_search"},
                "persona_text": "Arvid persona",
                "is_owner": True,
                "model_profile": "owner-profile",
            },
        )
    ]
    assert bus.messages == []


@pytest.mark.asyncio
async def test_owner_turn_preserves_a2a_principal_in_responder_context() -> None:
    policy = _FakePolicy()
    responder = _FakeResponder()
    bus = _FakeBus()

    await process_owner_turn(
        prompt="Answer the Hermes request.",
        chat_id="owner@lid",
        session_key=None,
        post_to_whatsapp=False,
        policy_adapter=policy,
        responder=responder,
        bus=bus,
        actor_principal="service:a2a",
        peer="hermes",
    )

    assert responder.calls[0][1]["sender_id"] == "service:a2a"
    assert responder.calls[0][1]["metadata"] == {
        "source": "hermes_owner_turn",
        "sender_id": "service:a2a",
        "a2a_peer": "hermes",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "expected_chat_id"),
    [("owner", "owner@lid"), ("Molty Python", "molty@g.us")],
)
async def test_a2a_delivery_resolves_target_inside_yeoman(
    target: str,
    expected_chat_id: str,
) -> None:
    policy = _FakeA2APolicy()
    responder = _FakeDeliveryResponder()

    result = await process_a2a_delivery(
        target=target,
        kind="voice",
        text="Kurze Testnachricht.",
        idempotency_key="hermes-test-1",
        peer="hermes",
        policy_adapter=policy,
        responder=responder,
    )

    assert result == {
        "target": target,
        "kind": "voice",
        "response": "Voice message delivered to whatsapp:molty@g.us.",
    }
    assert policy.events[0].chat_id == expected_chat_id
    assert policy.events[0].sender_id == "service:a2a"
    assert policy.events[0].raw_metadata == {
        "source": "hermes_a2a",
        "a2a_peer": "hermes",
        "target": target,
        "idempotency_key": "hermes-test-1",
    }
    assert responder.calls == [
        {
            "tool_name": "send_voice",
            "channel": "whatsapp",
            "chat_id": expected_chat_id,
            "text": "Kurze Testnachricht.",
            "session_key": "a2a:hermes-test-1",
            "principal": "service:a2a",
            "is_owner": False,
        }
    ]


@pytest.mark.asyncio
async def test_a2a_delivery_requires_target_tool_permission() -> None:
    policy = _FakeA2APolicy(allowed_tools=frozenset())
    responder = _FakeDeliveryResponder()

    with pytest.raises(PermissionError, match="send_voice"):
        await process_a2a_delivery(
            target="Molty Python",
            kind="voice",
            text="Nicht senden.",
            idempotency_key="hermes-test-2",
            peer="hermes",
            policy_adapter=policy,
            responder=responder,
        )

    assert responder.calls == []


@pytest.mark.asyncio
async def test_owner_turn_posts_only_when_explicitly_requested() -> None:
    policy = _FakePolicy()
    responder = _FakeResponder()
    bus = _FakeBus()

    result = await process_owner_turn(
        prompt="Send this through WhatsApp.",
        chat_id="owner@lid",
        session_key="whatsapp:owner@lid",
        post_to_whatsapp=True,
        policy_adapter=policy,
        responder=responder,
        bus=bus,
    )

    assert result["posted_to_whatsapp"] is True
    assert len(bus.messages) == 1
    assert bus.messages[0].channel == "whatsapp"
    assert bus.messages[0].chat_id == "owner@lid"
    assert bus.messages[0].content == "Yeoman answer"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chat_id", "session_key"),
    [
        ("not-owner@lid", None),
        ("owner@lid", "whatsapp:other@lid"),
    ],
)
async def test_owner_turn_rejects_non_owner_or_cross_session(chat_id, session_key) -> None:
    policy = _FakePolicy()
    responder = _FakeResponder()
    bus = _FakeBus()

    with pytest.raises(PermissionError):
        await process_owner_turn(
            prompt="Do not run this.",
            chat_id=chat_id,
            session_key=session_key,
            post_to_whatsapp=False,
            policy_adapter=policy,
            responder=responder,
            bus=bus,
        )

    assert responder.calls == []
    assert bus.messages == []


@pytest.mark.asyncio
async def test_owner_turn_respects_policy_rejection() -> None:
    policy = _FakePolicy(should_respond=False)
    responder = _FakeResponder()
    bus = _FakeBus()

    with pytest.raises(PermissionError, match="paused"):
        await process_owner_turn(
            prompt="Do not run this while paused.",
            chat_id="owner@lid",
            session_key=None,
            post_to_whatsapp=False,
            policy_adapter=policy,
            responder=responder,
            bus=bus,
        )

    assert responder.calls == []
