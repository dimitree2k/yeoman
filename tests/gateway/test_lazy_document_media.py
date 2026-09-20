import asyncio
import sys
import time
from types import SimpleNamespace

import pytest
from yeoman_gateway.agent.tools.media_history import MediaHistoryTool
from yeoman_gateway.media.document_cache import DocumentCache
from yeoman_gateway.media.document_processing import DocumentProcessor
from yeoman_gateway.media.lazy_resolver import LazyMediaResolver


def _record(
    cache: DocumentCache,
    path,
    *,
    message_id: str,
    kind: str,
    mime_type: str,
    file_name: str,
    size_bytes: int | None = None,
) -> int:
    return cache.record_media_item(
        channel="whatsapp",
        chat_id="docs@g.us",
        message_id=message_id,
        sender_id="sender@lid",
        sender_name="Frank",
        kind=kind,
        mime_type=mime_type,
        file_name=file_name,
        local_path=path,
        size_bytes=path.stat().st_size if size_bytes is None else size_bytes,
    )


def _metadata(message_id: str) -> dict[str, str]:
    return {
        "message_id": "question-1",
        "reply_to_message_id": message_id,
    }


class _Router:
    def resolve(self, task_key: str, channel: str):
        assert (task_key, channel) == ("vision.ocr_image", "whatsapp")
        return SimpleNamespace(kind="ocr", model="ocr-model")


class _Vision:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    async def ocr_image(self, path, profile):
        assert path.is_file()
        assert profile.model == "ocr-model"
        self.calls += 1
        return self.text


@pytest.mark.asyncio
async def test_real_resolver_pdf_cache_hit_reuses_extraction(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "report.pdf"
    path.write_bytes(b"%PDF-1.4\n")
    cache = DocumentCache(tmp_path / "document_cache.db")
    item_id = _record(
        cache,
        path,
        message_id="pdf-1",
        kind="document",
        mime_type="application/pdf",
        file_name="report.pdf",
    )
    cache.save_extraction(
        media_item_id=item_id,
        mode="pdf_text:page:1",
        content="Cached revenue: 42",
    )
    processor = DocumentProcessor(cache=cache)
    monkeypatch.setattr(
        processor,
        "_read_pdf_text",
        lambda path: pytest.fail("cache hit must not extract the PDF again"),
    )
    resolver = LazyMediaResolver(cache=cache, processor=processor)

    result = await resolver.resolve(
        channel="whatsapp",
        chat_id="docs@g.us",
        content="Was steht in dem PDF?",
        metadata=_metadata("pdf-1"),
    )

    assert result is not None
    assert result["mode"] == "pdf_text"
    assert result["content"] == "Cached revenue: 42"


@pytest.mark.asyncio
async def test_legacy_pdf_cache_row_is_not_used_as_bounded_inspection(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "report.pdf"
    path.write_bytes(b"%PDF-1.4\n")
    cache = DocumentCache(tmp_path / "document_cache.db")
    item_id = _record(
        cache,
        path,
        message_id="pdf-legacy-cache",
        kind="document",
        mime_type="application/pdf",
        file_name="report.pdf",
    )
    cache.save_extraction(
        media_item_id=item_id,
        mode="pdf_text",
        content="legacy whole-document text",
    )
    processor = DocumentProcessor(cache=cache)

    def read_pdf(requested_path, page_number):
        assert requested_path == path
        assert page_number == 1
        return "bounded first-page text", None

    monkeypatch.setattr(processor, "_read_pdf_text", read_pdf)
    resolver = LazyMediaResolver(cache=cache, processor=processor)

    result = await resolver.resolve(
        channel="whatsapp",
        chat_id="docs@g.us",
        content="Was steht in dem PDF?",
        metadata=_metadata("pdf-legacy-cache"),
    )

    assert result is not None
    assert result["content"] == "bounded first-page text"


@pytest.mark.asyncio
async def test_real_resolver_pdf_intake_keeps_metadata_without_inspection(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "report.pdf"
    path.write_bytes(b"%PDF-1.4\n")
    cache = DocumentCache(tmp_path / "document_cache.db")
    item_id = _record(
        cache,
        path,
        message_id="pdf-intake",
        kind="document",
        mime_type="application/pdf",
        file_name="report.pdf",
    )
    processor = DocumentProcessor(cache=cache)
    calls = []

    def read_pdf(requested_path):
        calls.append(requested_path)
        return "must not be read", 1

    monkeypatch.setattr(processor, "_read_pdf_text", read_pdf)
    resolver = LazyMediaResolver(cache=cache, processor=processor)

    result = await resolver.resolve(
        channel="whatsapp",
        chat_id="docs@g.us",
        content="report.pdf",
        metadata={"message_id": "pdf-intake"},
    )

    assert result is None
    assert calls == []
    assert cache.get_extraction(item_id, "pdf_text") is None


@pytest.mark.asyncio
async def test_real_resolver_uncached_pdf_uses_processor_and_populates_cache(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "report.pdf"
    path.write_bytes(b"%PDF-1.4\n")
    cache = DocumentCache(tmp_path / "document_cache.db")
    item_id = _record(
        cache,
        path,
        message_id="pdf-2",
        kind="document",
        mime_type="application/pdf",
        file_name="report.pdf",
    )
    processor = DocumentProcessor(cache=cache)
    calls = 0

    def read_pdf(requested_path, page_number):
        nonlocal calls
        calls += 1
        assert requested_path == path
        assert page_number == 1
        return "Revenue 2026: 42", 1

    monkeypatch.setattr(processor, "_read_pdf_text", read_pdf)
    resolver = LazyMediaResolver(cache=cache, processor=processor)

    result = await resolver.resolve(
        channel="whatsapp",
        chat_id="docs@g.us",
        content="Was steht in dem PDF?",
        metadata=_metadata("pdf-2"),
    )

    assert result is not None
    assert result["content"] == "Revenue 2026: 42"
    assert calls == 1
    cached = cache.get_extraction(item_id, "pdf_text:page:1")
    assert cached is not None
    assert cached.content == "Revenue 2026: 42"


def test_pdf_reader_is_limited_to_the_first_page_even_for_large_documents(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "large.pdf"
    path.write_bytes(b"%PDF-1.4\n")
    cache = DocumentCache(tmp_path / "document_cache.db")
    processor = DocumentProcessor(cache=cache, max_pdf_pages=12)
    seen_indices = []

    class _Pages:
        def __len__(self):
            raise AssertionError("page count must not be materialized")

        def __getitem__(self, selection):
            seen_indices.append(selection)
            assert selection == 0
            return SimpleNamespace(extract_text=lambda: "first page")

    reader = SimpleNamespace(pages=_Pages())
    monkeypatch.setitem(sys.modules, "pypdf", SimpleNamespace(PdfReader=lambda _: reader))

    text, page_count = processor._read_pdf_text(path)

    assert text == "first page"
    assert page_count is None
    assert seen_indices == [0]


def test_pdf_reader_uses_bounded_page_tree_lookup_without_len(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "large-page-tree.pdf"
    path.write_bytes(b"%PDF-1.4\n")
    cache = DocumentCache(tmp_path / "document_cache.db")
    processor = DocumentProcessor(cache=cache)

    class _Pages:
        def __len__(self):
            raise AssertionError("page count must not be materialized")

        def __getitem__(self, selection):
            raise AssertionError(f"page list access is unbounded: {selection!r}")

    page = SimpleNamespace(extract_text=lambda: "first page")
    page_ref = SimpleNamespace(get_object=lambda: page)

    class _PageNode:
        def __getitem__(self, key):
            assert key == "/Kids"
            return [page_ref]

        def get(self, key, default=None):
            return "/Pages" if key == "/Type" else default

    class _Reader:
        pages = _Pages()

        def _get_page_in_node(self, page_number):
            assert page_number == 0
            return _PageNode(), 0

    reader = _Reader()
    monkeypatch.setitem(sys.modules, "pypdf", SimpleNamespace(PdfReader=lambda _: reader))

    text, page_count = processor._read_pdf_text(path)

    assert text == "first page"
    assert page_count is None


@pytest.mark.asyncio
async def test_document_processor_rejects_empty_pdf_inspection_question(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "report.pdf"
    path.write_bytes(b"%PDF-1.4\n")
    cache = DocumentCache(tmp_path / "document_cache.db")
    _record(
        cache,
        path,
        message_id="pdf-empty-question",
        kind="document",
        mime_type="application/pdf",
        file_name="report.pdf",
    )
    processor = DocumentProcessor(cache=cache)
    monkeypatch.setattr(
        processor,
        "_read_pdf_text",
        lambda *_: pytest.fail("an empty PDF question must not inspect the file"),
    )
    item = cache.lookup_by_message("whatsapp", "docs@g.us", "pdf-empty-question")
    assert item is not None

    result = await processor.extract_for_question(item, "")

    assert result is not None
    assert result["mode"] == "skipped"
    assert "explicit" in result["content"].lower()


@pytest.mark.asyncio
async def test_media_history_requires_explicit_query_before_extracting(
    tmp_path,
) -> None:
    path = tmp_path / "report.pdf"
    path.write_bytes(b"%PDF-1.4\n")
    cache = DocumentCache(tmp_path / "document_cache.db")
    _record(
        cache,
        path,
        message_id="pdf-tool-empty-query",
        kind="document",
        mime_type="application/pdf",
        file_name="report.pdf",
    )

    class _Processor:
        calls = 0

        async def extract_for_question(self, item, question):
            self.calls += 1
            return {"mode": "pdf_text", "content": "must not inspect"}

    processor = _Processor()
    tool = MediaHistoryTool(cache=cache, processor=processor)
    tool.set_context("whatsapp", "docs@g.us")

    result = await tool.execute(
        message_id="pdf-tool-empty-query",
        extract=True,
    )

    assert processor.calls == 0
    assert "explicit" in result.lower()


@pytest.mark.asyncio
async def test_explicit_pdf_request_reads_only_the_named_page(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "report.pdf"
    path.write_bytes(b"%PDF-1.4\n")
    cache = DocumentCache(tmp_path / "document_cache.db")
    _record(
        cache,
        path,
        message_id="pdf-page-2",
        kind="document",
        mime_type="application/pdf",
        file_name="report.pdf",
    )
    processor = DocumentProcessor(cache=cache)
    calls = []

    def read_pdf(requested_path, page_number):
        calls.append((requested_path, page_number))
        return f"page {page_number}", 10

    monkeypatch.setattr(processor, "_read_pdf_text", read_pdf)
    resolver = LazyMediaResolver(cache=cache, processor=processor)

    result = await resolver.resolve(
        channel="whatsapp",
        chat_id="docs@g.us",
        content="Read page 2 from the PDF",
        metadata=_metadata("pdf-page-2"),
    )

    assert result is not None
    assert result["content"] == "page 2"
    assert calls == [(path, 2)]


@pytest.mark.asyncio
async def test_pdf_page_range_is_rejected_before_reader_work(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "report.pdf"
    path.write_bytes(b"%PDF-1.4\n")
    cache = DocumentCache(tmp_path / "document_cache.db")
    _record(
        cache,
        path,
        message_id="pdf-range",
        kind="document",
        mime_type="application/pdf",
        file_name="report.pdf",
    )
    processor = DocumentProcessor(cache=cache)
    monkeypatch.setattr(
        processor,
        "_read_pdf_text",
        lambda *_: pytest.fail("page ranges must be rejected before parsing"),
    )
    resolver = LazyMediaResolver(cache=cache, processor=processor)

    result = await resolver.resolve(
        channel="whatsapp",
        chat_id="docs@g.us",
        content="Read pages 1-3 from the PDF",
        metadata=_metadata("pdf-range"),
    )

    assert result is not None
    assert result["mode"] == "skipped"
    assert "one page" in result["content"]


@pytest.mark.asyncio
async def test_large_pdf_is_rejected_before_any_reader_work(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "huge.pdf"
    path.write_bytes(b"%PDF-1.4\n")
    cache = DocumentCache(tmp_path / "document_cache.db")
    _record(
        cache,
        path,
        message_id="pdf-huge",
        kind="document",
        mime_type="application/pdf",
        file_name="huge.pdf",
        size_bytes=50 * 1024 * 1024,
    )
    processor = DocumentProcessor(cache=cache, max_document_bytes=12 * 1024 * 1024)
    monkeypatch.setattr(
        processor,
        "_read_pdf_text",
        lambda *_: pytest.fail("oversized PDFs must be rejected before parsing"),
    )
    resolver = LazyMediaResolver(cache=cache, processor=processor)

    result = await resolver.resolve(
        channel="whatsapp",
        chat_id="docs@g.us",
        content="Was steht in dem PDF?",
        metadata=_metadata("pdf-huge"),
    )

    assert result is not None
    assert result["mode"] == "skipped"
    assert "too large" in result["content"]


@pytest.mark.asyncio
async def test_real_resolver_cached_ocr_does_not_call_vision_again(
    tmp_path,
) -> None:
    path = tmp_path / "scan.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n")
    cache = DocumentCache(tmp_path / "document_cache.db")
    item_id = _record(
        cache,
        path,
        message_id="image-1",
        kind="image",
        mime_type="image/png",
        file_name="scan.png",
    )
    cache.save_extraction(
        media_item_id=item_id,
        mode="ocr_image",
        content="Invoice total: 42 EUR",
    )
    vision = _Vision("must not be used")
    processor = DocumentProcessor(
        cache=cache,
        model_router=_Router(),
        vision_describer=vision,
    )
    resolver = LazyMediaResolver(cache=cache, processor=processor)

    result = await resolver.resolve(
        channel="whatsapp",
        chat_id="docs@g.us",
        content="Was steht auf dem Bild?",
        metadata=_metadata("image-1"),
    )

    assert result is not None
    assert result["mode"] == "ocr_image"
    assert result["content"] == "Invoice total: 42 EUR"
    assert vision.calls == 0


@pytest.mark.asyncio
async def test_real_resolver_processor_failure_returns_no_content(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"%PDF-1.4\n")
    cache = DocumentCache(tmp_path / "document_cache.db")
    _record(
        cache,
        path,
        message_id="pdf-broken",
        kind="document",
        mime_type="application/pdf",
        file_name="broken.pdf",
    )
    processor = DocumentProcessor(cache=cache)

    def fail_pdf(path, page_number):
        raise ValueError("broken PDF")

    monkeypatch.setattr(processor, "_read_pdf_text", fail_pdf)
    resolver = LazyMediaResolver(cache=cache, processor=processor)

    result = await resolver.resolve(
        channel="whatsapp",
        chat_id="docs@g.us",
        content="Was steht in dem PDF?",
        metadata=_metadata("pdf-broken"),
    )

    assert result is None


@pytest.mark.asyncio
async def test_real_resolver_and_processor_can_be_bounded_by_caller_timeout(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "slow.pdf"
    path.write_bytes(b"%PDF-1.4\n")
    cache = DocumentCache(tmp_path / "document_cache.db")
    _record(
        cache,
        path,
        message_id="pdf-slow",
        kind="document",
        mime_type="application/pdf",
        file_name="slow.pdf",
    )
    processor = DocumentProcessor(cache=cache)

    def slow_pdf(path, page_number):
        time.sleep(0.2)
        return "too late", 1

    monkeypatch.setattr(processor, "_read_pdf_text", slow_pdf)
    resolver = LazyMediaResolver(cache=cache, processor=processor)

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            resolver.resolve(
                channel="whatsapp",
                chat_id="docs@g.us",
                content="Was steht in dem PDF?",
                metadata=_metadata("pdf-slow"),
            ),
            timeout=0.01,
        )
