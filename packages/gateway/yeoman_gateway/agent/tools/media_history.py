"""Tool for searching and lazily extracting retained chat media."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from time import time
from typing import TYPE_CHECKING, Any, Callable, Protocol

from yeoman_gateway.agent.tools.base import Tool
from yeoman_gateway.media.document_cache import DocumentCache, MediaItem
from yeoman_gateway.media.document_processing import is_explicit_pdf_request

if TYPE_CHECKING:
    from yeoman_gateway.media.lazy_resolver import LazyMediaProcessor


class _Processor(Protocol):
    async def extract_for_question(self, item: MediaItem, question: str) -> dict[str, Any] | None:
        """Extract bounded media text for the supplied item."""


class MediaHistoryTool(Tool):
    """Search retained media metadata and optionally extract/OCR a selected item."""

    def __init__(
        self,
        *,
        cache: DocumentCache,
        processor: "_Processor | LazyMediaProcessor | None",
        group_resolver: Callable[[str], tuple[str | None, str | None]] | None = None,
        max_results: int = 8,
    ) -> None:
        self._cache = cache
        self._processor = processor
        self._group_resolver = group_resolver
        self._max_results = max(1, int(max_results))
        self._channel = ""
        self._chat_id = ""
        self._is_owner = False

    def set_context(self, channel: str, chat_id: str, *, is_owner: bool = False) -> None:
        self._channel = channel
        self._chat_id = chat_id
        self._is_owner = is_owner

    @property
    def _channel(self) -> str:
        from yeoman_gateway.processing.tool_context import tool_target
        return tool_target(default_channel=self._default_channel, default_chat_id=self._default_chat_id)[0]

    @_channel.setter
    def _channel(self, value: str) -> None:
        self._default_channel = value

    @property
    def _chat_id(self) -> str:
        from yeoman_gateway.processing.tool_context import tool_target
        return tool_target(default_channel=self._default_channel, default_chat_id=self._default_chat_id)[1]

    @_chat_id.setter
    def _chat_id(self, value: str) -> None:
        self._default_chat_id = value

    @property
    def _is_owner(self) -> bool:
        from yeoman_gateway.processing.tool_context import current_tool_context
        context = current_tool_context()
        return context.is_owner if context is not None else self._default_is_owner

    @_is_owner.setter
    def _is_owner(self, value: bool) -> None:
        self._default_is_owner = value

    @property
    def name(self) -> str:
        return "media_history"

    @property
    def description(self) -> str:
        if self._can_use_group_parameter:
            return (
                "Search retained chat media files such as images, screenshots, PDFs, and documents. "
                "Use when the user asks about a previously shared file or image. In an owner DM, "
                "an optional group parameter may search another WhatsApp group. Set extract=true "
                "only when the user asks what the media contains or asks to analyze it; otherwise "
                "return metadata only."
            )
        return (
            "Search retained chat media files such as images, screenshots, PDFs, and documents. "
            "Use when the user asks about a previously shared file or image in the current chat. "
            "Set extract=true only when the user asks what the media contains or asks to analyze it; "
            "otherwise return metadata only."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        properties: dict[str, Any] = {
            "message_id": {
                "type": "string",
                "description": "Optional exact WhatsApp message id of the media item.",
            },
            "sender": {
                "type": "string",
                "description": "Optional sender display-name hint, for example Frank or Maurice.",
            },
            "file_name": {
                "type": "string",
                "description": "Optional file-name hint for documents or PDFs.",
            },
            "kind": {
                "type": "string",
                "enum": ["image", "document"],
                "description": "Optional media kind filter.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum search results to return.",
                "minimum": 1,
                "maximum": 12,
            },
            "extract": {
                "type": "boolean",
                "description": (
                    "When true, lazily OCR/extract the single matching or most recent result. "
                    "Leave false for simple file lookup."
                ),
            },
            "query": {
                "type": "string",
                "description": "The user's content question, used only as extraction context.",
            },
        }
        if self._can_use_group_parameter:
            properties = {
                "group": {
                    "type": "string",
                    "description": (
                        "Optional owner DM only WhatsApp group alias/name/chat id to search."
                    ),
                },
                **properties,
            }
        return {
            "type": "object",
            "properties": properties,
            "required": [],
        }

    @property
    def _can_use_group_parameter(self) -> bool:
        return self._channel == "whatsapp" and self._is_owner and not self._chat_id.endswith("@g.us")

    async def execute(self, **kwargs: Any) -> str:
        if not self._channel or not self._chat_id:
            return "Error: no chat context set."

        target_channel, target_chat_id, err = self._resolve_target(kwargs.get("group"))
        if err is not None:
            return f"Error: {err}"

        message_id = str(kwargs.get("message_id") or "").strip()
        limit = max(1, min(12, int(kwargs.get("limit") or self._max_results)))

        from yeoman_gateway.processing.tool_context import tool_history_snapshot
        snapshot = tool_history_snapshot(selected=getattr(self, '_history_selected', False))
        if snapshot is not None and target_channel == 'whatsapp':
            from dataclasses import replace

            from yeoman_gateway.adapters.reply_archive_history import archive_row
            from yeoman_gateway.history.queries import HistoryQueries
            queries = HistoryQueries(snapshot)
            rows = queries.media(chat_id=target_chat_id, limit=limit, native_id=message_id or None)
            items = []
            for row in reversed(rows):
                item = self._cache.lookup_by_message(target_channel, target_chat_id, row['native_message_id'])
                if item is None:
                    continue
                current = archive_row(queries, row)
                item = replace(item, sender_id=current['sender_id'], sender_name=current['sender_name'])
                if kwargs.get('kind') and item.kind != kwargs['kind']:
                    continue
                if kwargs.get('sender') and str(kwargs['sender']).lower() not in (item.sender_name or '').lower():
                    continue
                if kwargs.get('file_name') and str(kwargs['file_name']).lower() not in (item.file_name or '').lower():
                    continue
                items.append(item)
        elif message_id:
            item = self._cache.lookup_by_message(target_channel, target_chat_id, message_id)
            items = [item] if item is not None else []
        else:
            items = self._cache.find_recent(
                channel=target_channel,
                chat_id=target_chat_id,
                kind=self._clean_optional(kwargs.get("kind")),
                sender_name_hint=self._clean_optional(kwargs.get("sender")),
                filename_hint=self._clean_optional(kwargs.get("file_name")),
                limit=limit,
            )

        if not items:
            return (
                "No cached media found for that request. Retained media can be read for "
                "indexed files within the 30-day cache; this item may predate media indexing "
                "or may not match the supplied group/sender/type."
            )

        lines = [f"Found {len(items)} cached media item(s):"]
        lines.extend(self._format_item(index, item) for index, item in enumerate(items, start=1))

        if bool(kwargs.get("extract")):
            if self._processor is None:
                lines.append("\nExtraction is unavailable because no media processor is configured.")
            else:
                question = str(kwargs.get("query") or "").strip()
                if not is_explicit_pdf_request(question):
                    lines.append("\nExtraction requires an explicit content question.")
                else:
                    extraction = await self._processor.extract_for_question(items[0], question)
                    if extraction is None:
                        lines.append("\nExtraction returned no content.")
                    else:
                        lines.append("\n[Media Extraction]")
                        mode = str(extraction.get("mode") or "unknown")
                        content = str(extraction.get("content") or "").strip()
                        lines.append(f"mode: {mode}")
                        lines.append(content or "(empty)")

        return "\n".join(lines)

    def _resolve_target(self, group: Any) -> tuple[str, str, str | None]:
        group_ref = str(group or "").strip()
        if not group_ref:
            return self._channel, self._chat_id, None
        if not self._is_owner:
            return self._channel, self._chat_id, "cross-chat media access is owner-only."
        if self._chat_id.endswith("@g.us"):
            return self._channel, self._chat_id, "cross-chat media access is only available from DMs."
        if self._group_resolver is None:
            return self._channel, self._chat_id, "WhatsApp group resolver is not configured."
        resolved_chat_id, err = self._group_resolver(group_ref)
        if err is not None or not resolved_chat_id:
            return self._channel, self._chat_id, err or "failed to resolve group"
        return "whatsapp", resolved_chat_id, None

    @staticmethod
    def _clean_optional(value: Any) -> str | None:
        text = str(value or "").strip()
        return text or None

    @staticmethod
    def _format_item(index: int, item: MediaItem) -> str:
        dt = datetime.fromtimestamp(item.timestamp).astimezone()
        name = item.sender_name or item.sender_id or "?"
        file_name = item.file_name or Path(item.local_path).name
        size = f", {item.size_bytes} bytes" if item.size_bytes is not None else ""
        expires_days = max(0, int((item.expires_at - time()) // 86400))
        return (
            f"{index}. {dt:%Y-%m-%d %H:%M} from {name}: {item.kind} "
            f"{file_name} ({item.mime_type or 'unknown'}{size}); "
            f"message_id={item.message_id}; expires_in_days={expires_days}"
        )
