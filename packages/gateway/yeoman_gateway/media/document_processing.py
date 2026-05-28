"""Lazy document and screenshot extraction for chat media."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from loguru import logger

from yeoman_gateway.media.document_cache import DocumentCache, MediaItem
from yeoman_gateway.media.router import ModelRouter
from yeoman_gateway.media.vision import VisionDescriber


class DocumentProcessor:
    """Process a cached media item only after a relevant question arrives."""

    def __init__(
        self,
        *,
        cache: DocumentCache,
        model_router: ModelRouter | None = None,
        vision_describer: VisionDescriber | None = None,
        max_document_bytes: int = 12 * 1024 * 1024,
        max_image_bytes: int = 8 * 1024 * 1024,
        max_pdf_pages: int = 12,
        max_prompt_chars: int = 6000,
    ) -> None:
        self.cache = cache
        self.model_router = model_router
        self.vision_describer = vision_describer
        self.max_document_bytes = max(1, int(max_document_bytes))
        self.max_image_bytes = max(1, int(max_image_bytes))
        self.max_pdf_pages = max(1, int(max_pdf_pages))
        self.max_prompt_chars = max(200, int(max_prompt_chars))

    async def extract_for_question(self, item: MediaItem, question: str) -> dict[str, Any] | None:
        del question
        mode = self._mode_for_item(item)
        cached = self.cache.get_extraction(item.id, mode)
        if cached is not None:
            return self._block_for_item(item, mode=mode, content=cached.content)

        path = Path(item.local_path).expanduser()
        if not path.is_file():
            return self._block_for_item(
                item,
                mode="skipped",
                content="The referenced media file is no longer available in the 30-day cache.",
            )

        size_bytes = int(item.size_bytes or path.stat().st_size)
        if mode == "ocr_image" and size_bytes > self.max_image_bytes:
            return self._block_for_item(
                item,
                mode="skipped",
                content="The referenced image is too large to OCR automatically.",
            )
        if mode != "ocr_image" and size_bytes > self.max_document_bytes:
            return self._block_for_item(
                item,
                mode="skipped",
                content="The referenced document is too large to extract automatically.",
            )

        if mode == "ocr_image":
            return await self._ocr_image(item, path)
        if mode == "pdf_text":
            return await self._extract_pdf_text(item, path)
        return self._block_for_item(
            item,
            mode="skipped",
            content="This document type is cached, but automatic extraction is not enabled for it.",
        )

    async def _ocr_image(self, item: MediaItem, path: Path) -> dict[str, Any] | None:
        if self.model_router is None or self.vision_describer is None:
            return None
        try:
            profile = self.model_router.resolve("vision.ocr_image", channel=item.channel)
        except KeyError as e:
            logger.warning("Skipping OCR due to missing route: {}", e)
            return None
        text = await self.vision_describer.ocr_image(path, profile)
        if not text:
            return None
        text = self._limit(text)
        self.cache.save_extraction(
            media_item_id=item.id,
            mode="ocr_image",
            content=text,
            char_count=len(text),
            page_count=1,
        )
        return self._block_for_item(item, mode="ocr_image", content=text)

    async def _extract_pdf_text(self, item: MediaItem, path: Path) -> dict[str, Any] | None:
        try:
            text, page_count = await asyncio.to_thread(self._read_pdf_text, path)
        except ImportError:
            logger.warning("pypdf not installed; cannot extract PDF text from {}", path)
            return self._block_for_item(
                item,
                mode="skipped",
                content="PDF extraction is unavailable because the PDF parser is not installed.",
            )
        except Exception as e:
            logger.warning("PDF extraction failed for {}: {}", path, e)
            return None

        if not text:
            return self._block_for_item(
                item,
                mode="skipped",
                content="No embedded text was found in this PDF. OCR fallback is not enabled for it.",
            )
        limited = self._limit(text)
        self.cache.save_extraction(
            media_item_id=item.id,
            mode="pdf_text",
            content=limited,
            char_count=len(limited),
            page_count=page_count,
        )
        return self._block_for_item(item, mode="pdf_text", content=limited)

    def _read_pdf_text(self, path: Path) -> tuple[str, int]:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        pages = reader.pages[: self.max_pdf_pages]
        chunks = []
        for page in pages:
            page_text = page.extract_text() or ""
            compact = "\n".join(line.rstrip() for line in page_text.splitlines()).strip()
            if compact:
                chunks.append(compact)
        return "\n\n".join(chunks).strip(), len(reader.pages)

    def _mode_for_item(self, item: MediaItem) -> str:
        mime = (item.mime_type or "").lower()
        name = (item.file_name or str(item.local_path)).lower()
        if item.kind == "image" or mime.startswith("image/"):
            return "ocr_image"
        if mime == "application/pdf" or name.endswith(".pdf"):
            return "pdf_text"
        return "document_text"

    def _block_for_item(self, item: MediaItem, *, mode: str, content: str) -> dict[str, Any]:
        return {
            "mode": mode,
            "content": self._limit(content),
            "source": {
                "message_id": item.message_id,
                "sender_name": item.sender_name,
                "file_name": item.file_name,
                "mime_type": item.mime_type,
                "kind": item.kind,
            },
        }

    def _limit(self, text: str) -> str:
        value = str(text or "").strip()
        if len(value) > self.max_prompt_chars:
            return value[: self.max_prompt_chars].rstrip() + "\n[truncated]"
        return value
