"""Tool that renders a meme and sends it as a photo to the current chat."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from yeoman_gateway.agent.tools.base import Tool
from yeoman_gateway.bus.events import OutboundMessage
from yeoman_gateway.history.live import HistoryPaused
from yeoman_gateway.media.document_cache import DocumentCache, MediaItem
from yeoman_gateway.media.memes.service import MemeError, MemeService

#: A photo posted this recently may be memed without replying to it.
_RECENT_PHOTO_SECONDS = 10 * 60

_GUIDANCE = (
    "Rules: keep it funny or absurd and short (a few words per caption, in the chat's "
    "language). Never spread disinformation and never put invented quotes in the mouth of "
    "a real person. Politics only as an obvious joke, never as a claim. Use a photo that "
    "shows people only when someone in the chat asked for exactly that, and never mock "
    "anyone's looks, body or identity. If a request breaks these rules, decline briefly "
    "instead of calling this tool."
)


class MakeMemeTool(Tool):
    """Render a template, chat-photo or generated-background meme into the current chat."""

    def __init__(
        self,
        *,
        service: MemeService,
        send_callback: Callable[[OutboundMessage], Awaitable[None]],
        media_cache: DocumentCache | None = None,
    ) -> None:
        self._service = service
        self._send_callback = send_callback
        self._media_cache = media_cache

    @property
    def name(self) -> str:
        return "make_meme"

    @property
    def description(self) -> str:
        modes = "'template' (a classic meme image) or 'photo' (a photo posted in this chat)"
        if self._service.backgrounds_available:
            modes += " or 'background' (a freshly generated picture, limited per day)"
        return (
            f"Make a meme and send it to the current chat. Modes: {modes}. Output: "
            "'sticker' for a short reaction (one or two words per caption, answering what "
            "was just said), otherwise 'photo'. "
            "Write the captions yourself unless the user dictated them. For 'photo', reply "
            "context selects the photo; photos are only taken from this chat and are kept "
            "about 30 days. Templates as id(number of caption boxes): "
            f"{self._service.template_catalog()}. Give exactly one caption per box, in order "
            f"(empty string to skip a box). {_GUIDANCE}"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        modes = ["template", "photo"]
        if self._service.backgrounds_available:
            modes.append("background")
        properties: dict[str, Any] = {
            "mode": {"type": "string", "enum": modes},
            "output": {
                "type": "string",
                "enum": ["photo", "sticker"],
                "description": "photo (default) or sticker for short reaction memes.",
            },
            "captions": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 4,
                "description": "Caption lines; for photo/background: [top, bottom].",
            },
            "template": {
                "type": "string",
                "description": "Template id for mode 'template', e.g. drake.",
            },
            "message_id": {
                "type": "string",
                "description": (
                    "Optional for mode 'photo': id of the photo message in this chat. "
                    "Defaults to the message the user replied to."
                ),
            },
        }
        if self._service.backgrounds_available:
            properties["background_prompt"] = {
                "type": "string",
                "description": (
                    "For mode 'background': short English scene description of the "
                    "picture, without any text in it."
                ),
            }
        return {"type": "object", "properties": properties, "required": ["mode", "captions"]}

    async def execute(self, **kwargs: Any) -> str:
        from yeoman_gateway.processing.tool_context import current_tool_context

        context = current_tool_context()
        if context is None or not context.chat_id:
            return "Error: no chat context set."
        if context.channel != "whatsapp":
            return "Error: memes are only supported in WhatsApp chats."

        mode = str(kwargs.get("mode") or "").strip()
        captions = kwargs.get("captions")
        if isinstance(captions, str):
            captions = [captions]
        output = str(kwargs.get("output") or "photo").strip()
        if output not in {"photo", "sticker"}:
            return f"Error: unknown meme output '{output}'."
        sticker = output == "sticker"
        reply_to: str | None = None
        try:
            if mode == "template":
                path = await self._service.from_template(
                    str(kwargs.get("template") or ""), captions, sticker=sticker
                )
            elif mode == "photo":
                item = self._find_photo(
                    context.chat_id,
                    str(kwargs.get("message_id") or "").strip() or context.reply_to_message_id,
                )
                if isinstance(item, str):
                    return item
                reply_to = item.message_id
                path = await self._service.from_photo(
                    Path(item.local_path), captions, sticker=sticker
                )
            elif mode == "background" and self._service.backgrounds_available:
                path = await self._service.from_background(
                    str(kwargs.get("background_prompt") or ""),
                    captions,
                    context.channel,
                    sticker=sticker,
                )
            else:
                return f"Error: unknown meme mode '{mode}'."
        except MemeError as exc:
            return f"Error: {exc}"

        try:
            await self._send_callback(
                OutboundMessage(
                    channel=context.channel,
                    chat_id=context.chat_id,
                    content="",
                    reply_to=reply_to,
                    media=[str(path)],
                )
            )
        except HistoryPaused:
            raise
        except Exception as exc:
            return f"Error: meme not delivered ({exc})"
        return (
            f"Meme {output} delivered to {context.channel}:{context.chat_id}. "
            "Do not describe it; at most add a very short comment."
        )

    def _find_photo(self, chat_id: str, message_id: str | None) -> MediaItem | str:
        if self._media_cache is None:
            return "Error: chat photos are not available for memes on this setup."
        if message_id:
            item = self._media_cache.lookup_by_message("whatsapp", chat_id, message_id)
            if item is None or item.kind != "image":
                return (
                    "Error: that message has no photo I can still use. Photos are kept about "
                    "30 days; tell the user it is gone or ask them to post it again."
                )
        else:
            recent = self._media_cache.find_recent(
                channel="whatsapp", chat_id=chat_id, kind="image", limit=1
            )
            item = recent[0] if recent else None
            if item is None or time.time() - item.timestamp > _RECENT_PHOTO_SECONDS:
                return (
                    "Error: no photo selected. Ask the user to reply to the photo they want memed."
                )
        if not Path(item.local_path).is_file():
            return "Error: that photo has already been deleted; ask the user to post it again."
        return item
