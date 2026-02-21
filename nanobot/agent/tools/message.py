"""Message tool for sending messages to users."""

from typing import Any, Awaitable, Callable

from nanobot.agent.tools.base import Tool
from nanobot.bus.events import OutboundMessage


class MessageTool(Tool):
    """Tool to send messages to users on chat channels."""

    def __init__(
        self,
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
        default_channel: str = "",
        default_chat_id: str = "",
        group_resolver: Callable[[str], tuple[str | None, str | None]] | None = None,
    ):
        self._send_callback = send_callback
        self._default_channel = default_channel
        self._default_chat_id = default_chat_id
        self._group_resolver = group_resolver

    def set_context(self, channel: str, chat_id: str) -> None:
        """Set the current message context."""
        self._default_channel = channel
        self._default_chat_id = chat_id

    def set_send_callback(self, callback: Callable[[OutboundMessage], Awaitable[None]]) -> None:
        """Set the callback for sending messages."""
        self._send_callback = callback

    def set_group_resolver(
        self,
        resolver: Callable[[str], tuple[str | None, str | None]] | None,
    ) -> None:
        """Set optional WhatsApp group resolver used by `group` parameter."""
        self._group_resolver = resolver

    @property
    def name(self) -> str:
        return "message"

    @property
    def description(self) -> str:
        return (
            "Send a text message to a specific channel/chat. "
            "For WhatsApp groups, you can pass `group` as alias/name/chat id."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The message content to send"
                },
                "channel": {
                    "type": "string",
                    "description": "Optional: target channel (telegram, discord, etc.)"
                },
                "chat_id": {
                    "type": "string",
                    "description": "Optional: target chat/user ID"
                },
                "group": {
                    "type": "string",
                    "description": (
                        "Optional (WhatsApp only): group alias/name/chat id "
                        "resolved to a @g.us chat"
                    ),
                }
            },
            "required": ["content"]
        }

    async def execute(
        self,
        content: str,
        channel: str | None = None,
        chat_id: str | None = None,
        group: str | None = None,
        **kwargs: Any
    ) -> str:
        del kwargs
        channel_explicit = str(channel or "").strip()
        chat_id_explicit = str(chat_id or "").strip()
        channel = channel_explicit or self._default_channel.strip()
        chat_id = chat_id_explicit
        group_ref = str(group or "").strip()

        if group_ref:
            if chat_id_explicit:
                return "Error: Use either `chat_id` or `group`, not both"
            if not channel:
                channel = "whatsapp"
            if channel != "whatsapp":
                return "Error: `group` is supported only for WhatsApp"
            if self._group_resolver is None:
                return "Error: WhatsApp group resolver is not configured"
            resolved_chat_id, err = self._group_resolver(group_ref)
            if err is not None or not resolved_chat_id:
                return f"Error: {err or 'failed to resolve group'}"
            chat_id = resolved_chat_id
        elif not chat_id:
            chat_id = self._default_chat_id.strip()

        if not channel or not chat_id:
            return "Error: No target channel/chat specified"

        if not self._send_callback:
            return "Error: Message sending not configured"

        msg = OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=content
        )

        try:
            await self._send_callback(msg)
            return f"Message sent to {channel}:{chat_id}"
        except Exception as e:
            return f"Error sending message: {str(e)}"
