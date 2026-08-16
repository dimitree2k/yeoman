import asyncio
import time
from types import SimpleNamespace

import pytest
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
        size_bytes=path.stat().st_size,
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
        mode="pdf_text",
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

    def read_pdf(requested_path):
        nonlocal calls
        calls += 1
        assert requested_path == path
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
    cached = cache.get_extraction(item_id, "pdf_text")
    assert cached is not None
    assert cached.content == "Revenue 2026: 42"


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

    def fail_pdf(path):
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

    def slow_pdf(path):
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
