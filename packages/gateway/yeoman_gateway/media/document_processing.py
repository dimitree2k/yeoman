"""Lazy document and screenshot extraction for chat media."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from loguru import logger

from yeoman_gateway.implicit_addressing import looks_like_question_or_request
from yeoman_gateway.media.document_cache import DocumentCache, MediaItem
from yeoman_gateway.media.router import ModelRouter
from yeoman_gateway.media.vision import VisionDescriber

_PDF_PAGE_RE = re.compile(r"(?i)\b(?:page|seite)\s+(\d+)\b")
_PDF_REQUEST_RE = re.compile(
    r"(?i)\b(?:analy[sz](?:e|ieren)?|check|extract|inspect|read|review|"
    r"summari[sz](?:e|ing|ieren)?|zusammenfass(?:en|ung)?)\b"
)
_PDF_PAGE_RANGE_RE = re.compile(
    r"(?i)\b(?:pages?|seiten?)\s+\d+\s*(?:[-–—,]|to|bis|and|und)\s*\d+\b"
    r"|\b(?:all|every|alle|mehrere)\s+(?:pages?|seiten?)\b"
)


def is_explicit_pdf_request(content: str) -> bool:
    """Return whether *content* explicitly asks to inspect PDF content."""
    compact = " ".join(str(content or "").split())
    return bool(compact and (looks_like_question_or_request(compact) or _PDF_REQUEST_RE.search(compact)))


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
        max_pdf_pages: int = 1,
        max_prompt_chars: int = 6000,
    ) -> None:
        self.cache = cache
        self.model_router = model_router
        self.vision_describer = vision_describer
        self.max_document_bytes = max(1, int(max_document_bytes))
        self.max_image_bytes = max(1, int(max_image_bytes))
        self.max_pdf_pages = min(1, max(1, int(max_pdf_pages)))
        self.max_prompt_chars = max(200, int(max_prompt_chars))

    async def extract_for_question(self, item: MediaItem, question: str) -> dict[str, Any] | None:
        mode = self._mode_for_item(item)
        page_number = 1
        cache_mode = mode
        if mode == "pdf_text":
            if not is_explicit_pdf_request(question):
                return self._block_for_item(
                    item,
                    mode="skipped",
                    content="PDF inspection requires an explicit content question.",
                )
            page_number = self._pdf_page_request(question)
            if page_number is None:
                return self._block_for_item(
                    item,
                    mode="skipped",
                    content="PDF inspection is limited to one page; please choose a single page.",
                )
            cache_mode = f"pdf_text:page:{page_number}"

        cached = self.cache.get_extraction(item.id, cache_mode)
        if cached is not None:
            return self._block_for_item(item, mode="pdf_text" if mode == "pdf_text" else mode, content=cached.content)

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
            return await self._extract_pdf_text(
                item,
                path,
                page_number=page_number,
                cache_mode=cache_mode,
            )
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

    async def _extract_pdf_text(
        self,
        item: MediaItem,
        path: Path,
        *,
        page_number: int = 1,
        cache_mode: str = "pdf_text:page:1",
    ) -> dict[str, Any] | None:
        try:
            text, page_count = await asyncio.to_thread(self._read_pdf_text, path, page_number)
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
                content=(
                    f"No embedded text was found on PDF page {page_number}. "
                    "OCR fallback is not enabled for it."
                ),
            )
        limited = self._limit(text)
        self.cache.save_extraction(
            media_item_id=item.id,
            mode=cache_mode,
            content=limited,
            char_count=len(limited),
            page_count=page_count,
        )
        return self._block_for_item(item, mode="pdf_text", content=limited)

    def _read_pdf_text(self, path: Path, page_number: int = 1) -> tuple[str, int | None]:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        start = max(0, int(page_number) - 1)
        try:
            page = self._pdf_page_at(reader, start)
        except IndexError:
            return "", None
        page_text = page.extract_text() or ""
        compact = "\n".join(line.rstrip() for line in page_text.splitlines()).strip()
        return compact, None

    @staticmethod
    def _pdf_page_at(reader: Any, page_index: int) -> Any:
        bounded_lookup = getattr(reader, "_get_page_in_node", None)
        if not callable(bounded_lookup):
            return reader.pages[page_index]

        node, child_index = bounded_lookup(page_index)
        if child_index < 0:
            if node.get("/Type") != "/Page":
                raise IndexError("PDF page index is out of range")
            page_ref = node
        else:
            page_ref = node["/Kids"][child_index]
        page = page_ref.get_object() if hasattr(page_ref, "get_object") else page_ref
        if hasattr(page, "extract_text"):
            return page

        from pypdf._page import PageObject
        from pypdf.generic import NameObject

        indirect_reference = getattr(page_ref, "indirect_reference", None)
        result = PageObject(reader, indirect_reference)
        result.update(page)
        inherited: dict[str, Any] = {}
        current = page
        while current is not None:
            for key in ("/Resources", "/MediaBox", "/CropBox", "/Rotate"):
                if key not in inherited and key in current:
                    inherited[key] = current[key]
            parent = current.get("/Parent")
            current = parent.get_object() if hasattr(parent, "get_object") else parent
        for key, value in inherited.items():
            result.setdefault(NameObject(key), value)
        return result

    @staticmethod
    def _pdf_page_request(question: str) -> int | None:
        compact = " ".join(str(question or "").split())
        if not compact or _PDF_PAGE_RANGE_RE.search(compact):
            return None if compact else 1
        pages = [int(value) for value in _PDF_PAGE_RE.findall(compact)]
        if len(pages) > 1:
            return None
        return max(1, pages[0]) if pages else 1

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
