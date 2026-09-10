"""Tool for deleting the assistant's own WhatsApp messages for everyone."""

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from yeoman_gateway.agent.tools.base import Tool
from yeoman_gateway.agent.tools.send_voice import (
    _is_resolvable_whatsapp_chat_id,
    _normalize_whatsapp_chat_id,
    _unresolved_whatsapp_target_error,
)
from yeoman_gateway.bus.events import OutboundMessage


@dataclass(frozen=True, slots=True)
class _DeleteMessageContext:
    channel: str
    chat_id: str
    reply_to_message_id: str
    is_owner: bool


class DeleteMessageTool(Tool):
    """Queue deletion of one exact WhatsApp message authored by the assistant."""

    def __init__(
        self,
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
        default_channel: str = "",
        default_chat_id: str = "",
        reply_to_message_id: str | None = None,
        group_resolver: Callable[[str], tuple[str | None, str | None]] | None = None,
    ) -> None:
        self._send_callback = send_callback
        self._context: ContextVar[_DeleteMessageContext] = ContextVar(
            "delete_message_context",
            default=_DeleteMessageContext(
                channel=default_channel,
                chat_id=default_chat_id,
                reply_to_message_id=str(reply_to_message_id or "").strip(),
                is_owner=False,
            ),
        )
        self._group_resolver = group_resolver

    def set_context(
        self,
        channel: str,
        chat_id: str,
        reply_to_message_id: str | None = None,
        is_owner: bool = False,
    ) -> None:
        """Set the current chat and optional quoted/replied-to message target."""
        self._context.set(
            _DeleteMessageContext(
                channel=channel,
                chat_id=chat_id,
                reply_to_message_id=str(reply_to_message_id or "").strip(),
                is_owner=bool(is_owner),
            )
        )

    def set_send_callback(self, callback: Callable[[OutboundMessage], Awaitable[None]]) -> None:
        """Set the callback used to queue the deletion request."""
        self._send_callback = callback

    def set_group_resolver(
        self,
        resolver: Callable[[str], tuple[str | None, str | None]] | None,
    ) -> None:
        """Set the optional WhatsApp group resolver used by ``group``."""
        self._group_resolver = resolver

    @property
    def name(self) -> str:
        return "delete_message"

    @property
    def description(self) -> str:
        return (
            "Delete one of the assistant's own WhatsApp messages for everyone. "
            "Use an exact message_id, or omit it when deleting the quoted/replied-to message. "
            "Only the owner may use this tool; never delete a message authored by another person."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Exact WhatsApp message ID; omit this only when the target is the "
                        "quoted/replied-to message"
                    ),
                },
                "channel": {
                    "type": "string",
                    "description": "Optional target channel; must be WhatsApp",
                },
                "chat_id": {
                    "type": "string",
                    "description": "Optional WhatsApp chat JID or phone number",
                },
                "group": {
                    "type": "string",
                    "description": "Optional WhatsApp group alias/name/chat JID",
                },
            },
        }

    async def execute(
        self,
        message_id: str | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
        group: str | None = None,
        **kwargs: Any,
    ) -> str:
        del kwargs
        context = self._context.get()

        if not context.is_owner:
            return "Error: Message deletion is restricted to the owner"

        channel_explicit = str(channel or "").strip()
        chat_id_explicit = str(chat_id or "").strip()
        resolved_channel = channel_explicit or context.channel.strip()
        resolved_chat_id: str | None = chat_id_explicit or None
        group_ref = str(group or "").strip()

        if group_ref:
            if chat_id_explicit:
                return "Error: Use either `chat_id` or `group`, not both"
            if not resolved_channel:
                resolved_channel = "whatsapp"
            if resolved_channel != "whatsapp":
                return "Error: `group` is supported only for WhatsApp"
            if self._group_resolver is None:
                return "Error: WhatsApp group resolver is not configured"
            resolved_chat_id, err = self._group_resolver(group_ref)
            if err is not None or not resolved_chat_id:
                return f"Error: {err or 'failed to resolve group'}"
        elif not resolved_chat_id:
            resolved_chat_id = context.chat_id.strip()

        if not resolved_channel:
            return "Error: No target channel/chat specified"
        if resolved_channel != "whatsapp":
            return "Error: delete_message only supports WhatsApp"
        if not resolved_chat_id:
            return "Error: No target channel/chat specified"
        if not _is_resolvable_whatsapp_chat_id(resolved_chat_id):
            return _unresolved_whatsapp_target_error(resolved_chat_id)
        resolved_chat_id = _normalize_whatsapp_chat_id(resolved_chat_id)

        message_id_explicit = str(message_id or "").strip()
        reply_target = context.reply_to_message_id
        if not message_id_explicit and reply_target:
            current_chat_id = context.chat_id.strip()
            if not _is_resolvable_whatsapp_chat_id(current_chat_id):
                return "Error: An explicit message_id is required when targeting a different chat"
            current_chat_id = _normalize_whatsapp_chat_id(current_chat_id)
            if current_chat_id != resolved_chat_id:
                return "Error: An explicit message_id is required when targeting a different chat"

        target_message_id = message_id_explicit or reply_target
        if not target_message_id:
            return (
                "Error: No message_id specified; quote/reply to the message or provide its exact ID"
            )
        if not self._send_callback:
            return "Error: Message deletion is not configured"

        request = OutboundMessage(
            channel="whatsapp",
            chat_id=resolved_chat_id,
            content="",
            metadata={"delete_message": {"message_id": target_message_id}},
        )
        try:
            await self._send_callback(request)
            return (
                f"Delete request queued for whatsapp:{resolved_chat_id}; "
                f"message_id={target_message_id}. Only Arvid's own WhatsApp messages can be deleted."
            )
        except Exception as e:
            return f"Error deleting message: {e}"
