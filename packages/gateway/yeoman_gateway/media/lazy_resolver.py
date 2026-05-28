"""Resolve user questions that refer to recently shared media."""

from __future__ import annotations

import re
from typing import Any, Protocol

from yeoman_gateway.media.document_cache import DocumentCache, MediaItem


class LazyMediaProcessor(Protocol):
    async def extract_for_question(self, item: MediaItem, question: str) -> dict[str, Any] | None:
        """Return a temporary retrieval block for *item* if processing succeeds."""


class LazyMediaResolver:
    """Find cached chat media only when the current turn actually asks for it."""

    def __init__(
        self,
        *,
        cache: DocumentCache,
        processor: LazyMediaProcessor | None,
        max_prompt_chars: int = 6000,
    ) -> None:
        self.cache = cache
        self.processor = processor
        self.max_prompt_chars = max(200, int(max_prompt_chars))

    def resolve_cached(
        self,
        *,
        channel: str,
        chat_id: str,
        content: str,
        metadata: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Resolve only already-cached extraction content.

        This synchronous path is used by tests and as a fast path before any
        paid extraction/OCR call.
        """
        item = self._resolve_item(channel=channel, chat_id=chat_id, content=content, metadata=metadata)
        if item is None:
            return None
        mode = self._mode_for_item(item)
        extraction = self.cache.get_extraction(item.id, mode)
        if extraction is None:
            return None
        return self._block_for_item(item, mode=mode, content=extraction.content)

    async def resolve(
        self,
        *,
        channel: str,
        chat_id: str,
        content: str,
        metadata: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        item = self._resolve_item(channel=channel, chat_id=chat_id, content=content, metadata=metadata)
        if item is None:
            return None
        if not self._has_direct_media_reference(item=item, metadata=metadata):
            return None

        mode = self._mode_for_item(item)
        cached = self.cache.get_extraction(item.id, mode)
        if cached is not None:
            return self._block_for_item(item, mode=mode, content=cached.content)

        if self.processor is None:
            return None
        return await self.processor.extract_for_question(item, content)

    def _resolve_item(
        self,
        *,
        channel: str,
        chat_id: str,
        content: str,
        metadata: dict[str, Any] | None,
    ) -> MediaItem | None:
        if metadata:
            current_message_id = str(metadata.get("message_id") or "").strip()
            if current_message_id:
                item = self.cache.lookup_by_message(channel, chat_id, current_message_id)
                if item is not None:
                    return item

            reply_to = str(
                metadata.get("reply_to_message_id") or metadata.get("reply_to") or ""
            ).strip()
            if reply_to:
                item = self.cache.lookup_by_message(channel, chat_id, reply_to)
                if item is not None:
                    return item

        sender_hint = self._sender_hint(content)
        items = self.cache.find_recent(
            channel=channel,
            chat_id=chat_id,
            sender_name_hint=sender_hint,
            limit=2,
        )
        if len(items) == 1:
            return items[0]
        return None

    @staticmethod
    def _has_direct_media_reference(
        *,
        item: MediaItem,
        metadata: dict[str, Any] | None,
    ) -> bool:
        if not metadata:
            return False
        current_message_id = str(metadata.get("message_id") or "").strip()
        reply_to = str(metadata.get("reply_to_message_id") or metadata.get("reply_to") or "").strip()
        return item.message_id in {current_message_id, reply_to}

    def _mode_for_item(self, item: MediaItem) -> str:
        mime = (item.mime_type or "").lower()
        name = (item.file_name or str(item.local_path)).lower()
        if item.kind == "image" or mime.startswith("image/"):
            return "ocr_image"
        if mime == "application/pdf" or name.endswith(".pdf"):
            return "pdf_text"
        return "document_text"

    def _block_for_item(self, item: MediaItem, *, mode: str, content: str) -> dict[str, Any]:
        text = str(content or "")
        if len(text) > self.max_prompt_chars:
            text = text[: self.max_prompt_chars].rstrip() + "\n[truncated]"
        return {
            "mode": mode,
            "content": text,
            "source": {
                "message_id": item.message_id,
                "sender_name": item.sender_name,
                "file_name": item.file_name,
                "mime_type": item.mime_type,
                "kind": item.kind,
            },
        }

    @staticmethod
    def _sender_hint(content: str) -> str | None:
        # Simple high-signal hint for "Frank's PDF" / "Maurice screenshot".
        match = re.search(r"\b([A-ZÄÖÜ][\wÄÖÜäöüß-]{2,})['’]?(?:s)?\s+(?:pdf|document|datei|file|screenshot|bild|image|foto)\b", content)
        if match:
            return match.group(1)
        return None
