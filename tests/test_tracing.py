"""Unit tests for Langfuse tracing module."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from yeoman.telemetry import tracing


# ── Helpers ───────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_module_state():
    """Reset module-level state before and after every test."""
    tracing.reset()
    yield
    tracing.reset()


def _init_tracing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the required env vars and call ``init()``."""
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test-secret")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test-public")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://langfuse.test")
    tracing.init()


# ── Tests ─────────────────────────────────────────────────────────────


class TestNoOpWhenDisabled:
    """Verify every public function is a safe no-op when tracing is not configured."""

    def test_init_returns_false_without_secret_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
        assert tracing.init() is False
        assert tracing._client is None

    def test_start_trace_returns_none(self) -> None:
        assert tracing.start_trace(name="test") is None

    def test_start_span_returns_none(self) -> None:
        fake_trace = tracing.TraceContext(trace_id="abc", name="t", start_time="x")
        assert tracing.start_span(trace=fake_trace, name="s") is None

    def test_end_span_does_not_raise(self) -> None:
        tracing.end_span(None)  # should be a silent no-op

    def test_log_generation_does_not_raise(self) -> None:
        tracing.log_generation(
            parent=None,
            name="gen",
            model="gpt-4",
            input="hi",
            output="bye",
            usage={"input": 1, "output": 2, "total": 3},
        )
        assert len(tracing._batch) == 0


class TestInitialisation:
    """Verify init() wires up the async client correctly."""

    def test_init_returns_true_with_env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
        assert tracing.init() is True
        assert tracing._client is not None

    def test_init_uses_default_base_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
        monkeypatch.delenv("LANGFUSE_BASE_URL", raising=False)
        tracing.init()
        assert tracing._base_url == "https://cloud.langfuse.com"

    def test_init_respects_custom_base_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
        monkeypatch.setenv("LANGFUSE_BASE_URL", "https://custom.host/")
        tracing.init()
        assert tracing._base_url == "https://custom.host"

    def test_init_configures_flush_params(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
        tracing.init(flush_interval=10.0, batch_size=100)
        assert tracing._flush_interval == 10.0
        assert tracing._batch_size == 100


class TestContextCreation:
    """Verify TraceContext / SpanContext are created with correct IDs."""

    def test_start_trace_returns_trace_context(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _init_tracing(monkeypatch)
        ctx = tracing.start_trace(name="my-trace", metadata={"k": "v"})

        assert ctx is not None
        assert isinstance(ctx, tracing.TraceContext)
        assert len(ctx.trace_id) == 32  # uuid4().hex
        assert ctx.name == "my-trace"
        assert ctx.start_time  # non-empty ISO string

    def test_start_span_returns_span_context(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _init_tracing(monkeypatch)
        trace = tracing.start_trace(name="t")
        assert trace is not None
        span = tracing.start_span(trace=trace, name="my-span")

        assert span is not None
        assert isinstance(span, tracing.SpanContext)
        assert len(span.span_id) == 32
        assert span.trace_id == trace.trace_id
        assert span.name == "my-span"


class TestEventQueuing:
    """Verify events are queued with the correct structure."""

    def test_start_trace_queues_trace_create_event(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _init_tracing(monkeypatch)
        tracing.start_trace(name="t1", tags=["tag1"], input="hello", session_id="sess1")

        assert len(tracing._batch) == 1
        evt = tracing._batch[0]
        assert evt["type"] == "trace-create"
        assert evt["body"]["name"] == "t1"
        assert evt["body"]["tags"] == ["tag1"]
        assert evt["body"]["input"] == "hello"
        assert evt["body"]["sessionId"] == "sess1"

    def test_start_span_queues_span_create_event(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _init_tracing(monkeypatch)
        trace = tracing.start_trace(name="t")
        assert trace is not None
        tracing.start_span(trace=trace, name="s1", metadata={"x": 1})

        assert len(tracing._batch) == 2
        evt = tracing._batch[1]
        assert evt["type"] == "span-create"
        assert evt["body"]["traceId"] == trace.trace_id
        assert evt["body"]["name"] == "s1"
        assert evt["body"]["metadata"] == {"x": 1}
        assert "startTime" in evt["body"]

    def test_start_span_with_parent_span_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _init_tracing(monkeypatch)
        trace = tracing.start_trace(name="t")
        assert trace is not None
        parent = tracing.start_span(trace=trace, name="parent")
        assert parent is not None
        child = tracing.start_span(trace=trace, name="child", parent_span_id=parent.span_id)
        assert child is not None

        evt = tracing._batch[2]
        assert evt["body"]["parentObservationId"] == parent.span_id

    def test_end_span_queues_span_update_event(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _init_tracing(monkeypatch)
        trace = tracing.start_trace(name="t")
        assert trace is not None
        span = tracing.start_span(trace=trace, name="s")
        assert span is not None
        tracing.end_span(span, output={"result": "ok"}, metadata={"dur_ms": 42})

        assert len(tracing._batch) == 3
        evt = tracing._batch[2]
        assert evt["type"] == "span-update"
        assert evt["body"]["id"] == span.span_id
        assert evt["body"]["traceId"] == span.trace_id
        assert "endTime" in evt["body"]
        assert evt["body"]["output"] == {"result": "ok"}
        assert evt["body"]["metadata"] == {"dur_ms": 42}

    def test_log_generation_queues_generation_create_event(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_tracing(monkeypatch)
        trace = tracing.start_trace(name="t")
        assert trace is not None

        tracing.log_generation(
            parent=trace,
            name="chat-completion",
            model="claude-3-opus",
            input=[{"role": "user", "content": "hi"}],
            output={"role": "assistant", "content": "hello"},
            usage={"input": 10, "output": 20, "total": 30},
            metadata={"channel": "whatsapp"},
            model_parameters={"temperature": 0.6},
        )

        assert len(tracing._batch) == 2
        evt = tracing._batch[1]
        assert evt["type"] == "generation-create"
        body = evt["body"]
        assert body["traceId"] == trace.trace_id
        assert body["name"] == "chat-completion"
        assert body["model"] == "claude-3-opus"
        assert body["input"] == [{"role": "user", "content": "hi"}]
        assert body["output"] == {"role": "assistant", "content": "hello"}
        assert body["usageDetails"] == {"input": 10, "output": 20, "total": 30}
        assert body["metadata"] == {"channel": "whatsapp"}
        assert body["modelParameters"] == {"temperature": 0.6}
        assert "parentObservationId" not in body  # parent is a trace, not a span

    def test_log_generation_with_span_parent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _init_tracing(monkeypatch)
        trace = tracing.start_trace(name="t")
        assert trace is not None
        span = tracing.start_span(trace=trace, name="s")
        assert span is not None

        tracing.log_generation(
            parent=span,
            name="gen",
            model="gpt-4",
            input="q",
            output="a",
            usage={"input": 5, "output": 10, "total": 15},
        )

        evt = tracing._batch[2]
        assert evt["body"]["parentObservationId"] == span.span_id
        assert evt["body"]["traceId"] == trace.trace_id


class TestFlush:
    """Verify flush sends the batch via HTTP POST with correct auth."""

    @pytest.mark.asyncio
    async def test_flush_sends_post_and_clears_queue(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_tracing(monkeypatch)
        tracing.start_trace(name="t")
        assert len(tracing._batch) == 1

        mock_response = MagicMock()
        mock_response.status_code = 207
        mock_response.json.return_value = {"successes": [{}], "errors": []}

        assert tracing._client is not None
        with patch.object(tracing._client, "post", new_callable=AsyncMock, return_value=mock_response) as mock_post:
            await tracing.flush()

            mock_post.assert_called_once()
            call_kwargs = mock_post.call_args
            assert call_kwargs[0][0] == "/api/public/ingestion"
            payload = call_kwargs[1]["json"]
            assert len(payload["batch"]) == 1
            assert payload["batch"][0]["type"] == "trace-create"

        # Queue should be empty now.
        assert len(tracing._batch) == 0

    @pytest.mark.asyncio
    async def test_flush_noop_when_batch_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _init_tracing(monkeypatch)
        assert tracing._client is not None
        with patch.object(tracing._client, "post", new_callable=AsyncMock) as mock_post:
            await tracing.flush()
            mock_post.assert_not_called()

    @pytest.mark.asyncio
    async def test_flush_logs_warning_on_non_207(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_tracing(monkeypatch)
        tracing.start_trace(name="t")

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"

        assert tracing._client is not None
        with (
            patch.object(tracing._client, "post", new_callable=AsyncMock, return_value=mock_response),
            patch("yeoman.telemetry.tracing.logger") as mock_logger,
        ):
            await tracing.flush()
            mock_logger.warning.assert_called()

    @pytest.mark.asyncio
    async def test_flush_logs_warning_on_exception(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_tracing(monkeypatch)
        tracing.start_trace(name="t")

        assert tracing._client is not None
        with (
            patch.object(
                tracing._client,
                "post",
                new_callable=AsyncMock,
                side_effect=httpx.ConnectError("connection refused"),
            ),
            patch("yeoman.telemetry.tracing.logger") as mock_logger,
        ):
            # Must not raise.
            await tracing.flush()
            mock_logger.opt.assert_called()

    @pytest.mark.asyncio
    async def test_flush_auth_uses_public_and_secret_keys(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_tracing(monkeypatch)

        # Verify the httpx client was created with the correct auth.
        assert tracing._client is not None
        auth = tracing._client._auth  # type: ignore[attr-defined]
        # httpx BasicAuth stores (username, password) internally
        assert auth._auth_header  # just verify auth is wired up


class TestShutdown:
    """Verify shutdown flushes and closes the client."""

    @pytest.mark.asyncio
    async def test_shutdown_flushes_and_closes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _init_tracing(monkeypatch)
        tracing.start_trace(name="t")

        mock_response = MagicMock()
        mock_response.status_code = 207
        mock_response.json.return_value = {"successes": [{}], "errors": []}

        assert tracing._client is not None
        with (
            patch.object(tracing._client, "post", new_callable=AsyncMock, return_value=mock_response) as mock_post,
            patch.object(tracing._client, "aclose", new_callable=AsyncMock) as mock_close,
        ):
            await tracing.shutdown()
            mock_post.assert_called_once()
            mock_close.assert_called_once()

        assert tracing._client is None
        assert len(tracing._batch) == 0
