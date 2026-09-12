"""Owner-only synthetic WhatsApp turns from local IPC callers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from yeoman_gateway.bus.events import OutboundMessage
from yeoman_gateway.core.models import InboundEvent
from yeoman_gateway.processing.dispatch import SERVICE_PRINCIPALS

_MAX_PROMPT_CHARS = 20_000
_MAX_CHAT_ID_CHARS = 256
_MAX_TARGET_CHARS = 200
_MAX_IDEMPOTENCY_KEY_CHARS = 200
_A2A_SOURCE = "hermes_a2a"


class _PolicyAdapter(Protocol):
    def owner_recipients(self, channel: str) -> list[str]: ...

    def evaluate(self, event: InboundEvent) -> Any: ...


class _A2APolicyAdapter(_PolicyAdapter, Protocol):
    def resolve_whatsapp_group(self, reference: str) -> tuple[str | None, str | None]: ...


class _Responder(Protocol):
    async def process_direct(self, content: str, **kwargs: Any) -> str: ...


class _A2AResponder(Protocol):
    async def execute_delivery(self, **kwargs: Any) -> str: ...


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
    outbound_dispatch: "Callable[[OutboundMessage], Awaitable[Any]] | None" = None,
    actor_principal: str | None = None,
    peer: str | None = None,
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

    responder_kwargs: dict[str, Any] = {
        "session_key": canonical_session_key,
        "channel": "whatsapp",
        "chat_id": chat_id,
        "allowed_tools": set(decision.allowed_tools),
        "persona_text": decision.persona_text,
        "is_owner": True,
        "model_profile": decision.model_profile,
    }
    principal = str(actor_principal or "").strip()
    peer_name = str(peer or "").strip()
    if principal or peer_name:
        principal = principal or chat_id
        responder_kwargs["sender_id"] = principal
        responder_kwargs["metadata"] = {
            "source": "hermes_owner_turn",
            "sender_id": principal,
            "a2a_peer": peer_name,
        }
    response = await responder.process_direct(prompt, **responder_kwargs)
    response = str(response or "")

    posted = False
    if post_to_whatsapp:
        message = OutboundMessage(channel="whatsapp", chat_id=chat_id, content=response)
        # An owner turn posts as the verified owner principal; for a chat that the new
        # processing mode owns this goes through the effect gateway instead of the raw bus.
        if outbound_dispatch is not None:
            await outbound_dispatch(message)
        else:
            await bus.publish_outbound(message)
        posted = True

    return {
        "response": response,
        "session_key": canonical_session_key,
        "posted_to_whatsapp": posted,
    }


async def process_a2a_delivery(
    *,
    target: str,
    kind: str,
    text: str,
    idempotency_key: str,
    peer: str,
    policy_adapter: _A2APolicyAdapter,
    responder: _A2AResponder,
) -> dict[str, str]:
    """Resolve and execute one explicit Hermes -> WhatsApp delivery.

    Hermes supplies only a logical target. Yeoman resolves the owner/group, evaluates
    the target policy for ``service:a2a``, and invokes the existing delivery tool.
    """
    target = str(target or "").strip()
    if not target or len(target) > _MAX_TARGET_CHARS:
        raise ValueError("A2A target is invalid")
    if "@" in target:
        raise ValueError("A2A target must be owner or a group alias, not a JID")

    kind = str(kind or "").strip().lower()
    tool_name = {"voice": "send_voice", "text": "message"}.get(kind)
    if tool_name is None:
        raise ValueError("A2A kind must be voice or text")

    text = str(text or "").strip()
    if not text or len(text) > _MAX_PROMPT_CHARS:
        raise ValueError("A2A text is invalid")

    idempotency_key = str(idempotency_key or "").strip()
    if not idempotency_key or len(idempotency_key) > _MAX_IDEMPOTENCY_KEY_CHARS:
        raise ValueError("A2A idempotency_key is invalid")

    if target.lower() == "owner":
        owners = [str(item).strip() for item in policy_adapter.owner_recipients("whatsapp")]
        owners = [item for item in owners if item]
        if len(owners) != 1:
            raise PermissionError("A2A owner target requires exactly one WhatsApp owner")
        chat_id = owners[0]
    else:
        chat_id, error = policy_adapter.resolve_whatsapp_group(target)
        if error or not chat_id:
            raise PermissionError(error or f"A2A target '{target}' could not be resolved")
        chat_id = str(chat_id).strip()
        if not chat_id.endswith("@g.us"):
            raise PermissionError("A2A group target did not resolve to a WhatsApp group")

    principal = SERVICE_PRINCIPALS["a2a"]
    event = InboundEvent(
        channel="whatsapp",
        chat_id=chat_id,
        sender_id=principal,
        content=text,
        is_group=chat_id.endswith("@g.us"),
        mentioned_bot=True,
        reply_to_bot=True,
        raw_metadata={
            "source": _A2A_SOURCE,
            "a2a_peer": str(peer or "").strip(),
            "target": target,
            "idempotency_key": idempotency_key,
        },
    )
    decision = policy_adapter.evaluate(event)
    if not decision.accept_message or not decision.should_respond:
        raise PermissionError(f"A2A target policy rejected delivery: {decision.reason}")
    if tool_name not in decision.allowed_tools:
        raise PermissionError(f"A2A target policy does not allow {tool_name}")

    response = str(
        await responder.execute_delivery(
            tool_name=tool_name,
            channel="whatsapp",
            chat_id=chat_id,
            text=text,
            session_key=f"a2a:{idempotency_key}",
            principal=principal,
            is_owner=bool(decision.is_owner),
        )
        or ""
    ).strip()
    if response.startswith("Error:"):
        raise RuntimeError(response)
    return {"target": target, "kind": kind, "response": response}
