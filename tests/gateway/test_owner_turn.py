from __future__ import annotations

from types import SimpleNamespace

import pytest
from yeoman_gateway.ipc.owner_turn import process_owner_turn


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
