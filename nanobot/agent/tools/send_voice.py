"""Voice message tool for out-of-band TTS delivery."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from nanobot.agent.tools.base import Tool


@dataclass(frozen=True, slots=True)
class VoiceSendRequest:
    """Typed request envelope for voice sending callbacks."""

    channel: str
    chat_id: str
    content: str
    voice: str | None = None
    tts_route: str | None = None
    reply_to: str | None = None
    max_sentences: int | None = None
    max_chars: int | None = None
    verbatim: bool = False


class SendVoiceTool(Tool):
    """Tool for sending synthesized voice notes to chat channels."""

    def __init__(
        self,
        send_callback: Callable[[VoiceSendRequest], Awaitable[str]] | None = None,
        default_channel: str = "",
        default_chat_id: str = "",
        group_resolver: Callable[[str], tuple[str | None, str | None]] | None = None,
    ) -> None:
        self._send_callback = send_callback
        self._default_channel = default_channel
        self._default_chat_id = default_chat_id
        self._group_resolver = group_resolver

    def set_context(self, channel: str, chat_id: str) -> None:
        """Set current default channel/chat context."""
        self._default_channel = channel
        self._default_chat_id = chat_id

    def set_send_callback(self, callback: Callable[[VoiceSendRequest], Awaitable[str]]) -> None:
        """Set callback used to execute voice delivery."""
        self._send_callback = callback

    def set_group_resolver(
        self,
        resolver: Callable[[str], tuple[str | None, str | None]] | None,
    ) -> None:
        """Set optional WhatsApp group resolver used by `group` parameter."""
        self._group_resolver = resolver

    @property
    def name(self) -> str:
        return "send_voice"

    @property
    def description(self) -> str:
        return (
            "Synthesize text-to-speech and send as a voice note. "
            "Supports explicit chat_id or WhatsApp group alias/name."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "Text content to synthesize and send",
                },
                "channel": {
                    "type": "string",
                    "description": "Optional: target channel (defaults to current context)",
                },
                "chat_id": {
                    "type": "string",
                    "description": "Optional: target chat id",
                },
                "group": {
                    "type": "string",
                    "description": (
                        "Optional (WhatsApp only): group alias/name/chat id "
                        "resolved to a @g.us chat"
                    ),
                },
                "voice": {
                    "type": "string",
                    "description": "Optional: voice id/name for TTS backend",
                },
                "tts_route": {
                    "type": "string",
                    "description": "Optional: model route key, e.g. whatsapp.tts.speak",
                },
                "reply_to": {
                    "type": "string",
                    "description": "Optional: message id to reply to",
                },
                "max_sentences": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 12,
                    "description": "Optional: sentence cap before synthesis",
                },
                "max_chars": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 2000,
                    "description": "Optional: character cap before synthesis",
                },
                "verbatim": {
                    "type": "boolean",
                    "description": "Optional: preserve raw text without normalization/truncation",
                },
            },
            "required": ["content"],
        }

    async def execute(
        self,
        content: str,
        channel: str | None = None,
        chat_id: str | None = None,
        group: str | None = None,
        voice: str | None = None,
        tts_route: str | None = None,
        reply_to: str | None = None,
        max_sentences: int | None = None,
        max_chars: int | None = None,
        verbatim: bool | None = None,
        **kwargs: Any,
    ) -> str:
        del kwargs

        channel_explicit = str(channel or "").strip()
        chat_id_explicit = str(chat_id or "").strip()
        resolved_channel = channel_explicit or self._default_channel.strip()
        resolved_chat_id = chat_id_explicit
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
            group_chat_id, err = self._group_resolver(group_ref)
            if err is not None or not group_chat_id:
                return f"Error: {err or 'failed to resolve group'}"
            resolved_chat_id = group_chat_id
        elif not resolved_chat_id:
            resolved_chat_id = self._default_chat_id.strip()

        if not resolved_channel or not resolved_chat_id:
            return "Error: No target channel/chat specified"
        if not self._send_callback:
            return "Error: Voice sending is not configured"

        request = VoiceSendRequest(
            channel=resolved_channel,
            chat_id=resolved_chat_id,
            content=str(content or ""),
            voice=str(voice).strip() if voice is not None else None,
            tts_route=str(tts_route).strip() if tts_route is not None else None,
            reply_to=str(reply_to).strip() if reply_to is not None else None,
            max_sentences=max_sentences,
            max_chars=max_chars,
            verbatim=bool(verbatim),
        )

        try:
            return await self._send_callback(request)
        except Exception as e:
            return f"Error sending voice: {e}"
