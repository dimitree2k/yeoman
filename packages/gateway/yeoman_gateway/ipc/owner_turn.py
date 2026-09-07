"""Owner-only synthetic WhatsApp turns from local IPC callers."""

from __future__ import annotations

from typing import Any, Protocol

from yeoman_gateway.bus.events import OutboundMessage
from yeoman_gateway.core.models import InboundEvent

_MAX_PROMPT_CHARS = 20_000
_MAX_CHAT_ID_CHARS = 256


class _PolicyAdapter(Protocol):
    def owner_recipients(self, channel: str) -> list[str]: ...

    def evaluate(self, event: InboundEvent) -> Any: ...


class _Responder(Protocol):
    async def process_direct(self, content: str, **kwargs: Any) -> str: ...


class _Bus(Protocol):
    async def publish_outbound(self, message: OutboundMessage) -> Any: ...


async def process_owner_turn(
    *,
    prompt: str,
    chat_id: str,
    session_key: str | None,
    post_to_whatsapp: bool,
    policy_adapter: _PolicyAdapter,
    responder: _Responder,
    bus: _Bus,
) -> dict[str, object]:
    """Run one owner turn in the canonical WhatsApp session.

    The Unix socket is already user-only (0600), but the command still validates the
    target against Yeoman's configured WhatsApp owners. This keeps the Hermes bridge
    from becoming a general session-control interface.
    """
    prompt = str(prompt or "").strip()
    if not prompt:
        raise ValueError("prompt cannot be empty")
    if len(prompt) > _MAX_PROMPT_CHARS:
        raise ValueError(f"prompt exceeds {_MAX_PROMPT_CHARS} characters")

    chat_id = str(chat_id or "").strip()
    if not chat_id or len(chat_id) > _MAX_CHAT_ID_CHARS:
        raise ValueError("chat_id is invalid")

    owner_recipients = {
        str(recipient).strip()
        for recipient in policy_adapter.owner_recipients("whatsapp")
        if str(recipient).strip()
    }
    if chat_id not in owner_recipients:
        raise PermissionError("chat_id is not an owner WhatsApp recipient")

    canonical_session_key = f"whatsapp:{chat_id}"
    supplied_session_key = str(session_key or "").strip() or canonical_session_key
    if supplied_session_key != canonical_session_key:
        raise PermissionError("session_key must match the owner WhatsApp chat")

    event = InboundEvent(
        channel="whatsapp",
        chat_id=chat_id,
        sender_id=chat_id,
        content=prompt,
        raw_metadata={"source": "hermes_owner_turn"},
    )
    decision = policy_adapter.evaluate(event)
    if not decision.is_owner:
        raise PermissionError("owner policy rejected this WhatsApp target")
    if not decision.accept_message or not decision.should_respond:
        raise PermissionError(f"owner policy rejected the turn: {decision.reason}")

    response = await responder.process_direct(
        prompt,
        session_key=canonical_session_key,
        channel="whatsapp",
        chat_id=chat_id,
        allowed_tools=set(decision.allowed_tools),
        persona_text=decision.persona_text,
        is_owner=True,
        model_profile=decision.model_profile,
    )
    response = str(response or "")

    posted = False
    if post_to_whatsapp:
        await bus.publish_outbound(
            OutboundMessage(channel="whatsapp", chat_id=chat_id, content=response)
        )
        posted = True

    return {
        "response": response,
        "session_key": canonical_session_key,
        "posted_to_whatsapp": posted,
    }
