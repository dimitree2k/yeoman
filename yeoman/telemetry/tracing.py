"""Langfuse tracing via REST API.

Langfuse Python SDK is incompatible with Python 3.14, so we hit the
ingestion endpoint directly with httpx.  Every public function is a
safe no-op when ``LANGFUSE_SECRET_KEY`` is not set.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from loguru import logger

# ── Module-level state ────────────────────────────────────────────────

_client: httpx.AsyncClient | None = None
_batch: list[dict[str, Any]] = []
_flush_task: asyncio.Task[None] | None = None
_flush_interval: float = 5.0
_batch_size: int = 50
_base_url: str = "https://cloud.langfuse.com"


# ── Dataclasses ───────────────────────────────────────────────────────


@dataclass
class TraceContext:
    """Opaque handle returned by :func:`start_trace`."""

    trace_id: str
    name: str
    start_time: str


@dataclass
class SpanContext:
    """Opaque handle returned by :func:`start_span`."""

    span_id: str
    trace_id: str
    name: str
    start_time: str


# ── Initialisation ────────────────────────────────────────────────────


def init(
    *,
    flush_interval: float = 5.0,
    batch_size: int = 50,
) -> bool:
    """Bootstrap the Langfuse tracing client from environment variables.

    Returns ``True`` if tracing is now active, ``False`` otherwise.
    """
    global _client, _flush_interval, _batch_size, _base_url  # noqa: PLW0603

    secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "")
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "")

    if not secret_key:
        logger.debug("LANGFUSE_SECRET_KEY not set — tracing disabled")
        return False

    _base_url = os.environ.get("LANGFUSE_BASE_URL", "https://cloud.langfuse.com").rstrip("/")
    _flush_interval = flush_interval
    _batch_size = batch_size

    _client = httpx.AsyncClient(
        base_url=_base_url,
        auth=(public_key, secret_key),
        timeout=httpx.Timeout(10.0),
    )
    logger.info("Langfuse tracing enabled (base_url={})", _base_url)
    return True


# ── Helpers ───────────────────────────────────────────────────────────


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _uuid() -> str:
    return uuid.uuid4().hex


def _enqueue(event: dict[str, Any]) -> None:
    """Append *event* to the batch and maybe trigger a flush."""
    _batch.append(event)
    _maybe_start_flush_loop()
    if len(_batch) >= _batch_size:
        # Schedule an immediate flush without blocking the caller.
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_flush())
        except RuntimeError:
            pass  # no running loop — periodic task will pick it up


def _maybe_start_flush_loop() -> None:
    """Lazily create the periodic flush background task."""
    global _flush_task  # noqa: PLW0603
    if _flush_task is not None and not _flush_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
        _flush_task = loop.create_task(_periodic_flush())
    except RuntimeError:
        pass  # no event loop yet — will retry on next enqueue


async def _periodic_flush() -> None:
    """Flush the batch every ``_flush_interval`` seconds."""
    while _client is not None:
        await asyncio.sleep(_flush_interval)
        await _flush()


async def _flush() -> None:
    """Send all queued events to Langfuse and clear the queue."""
    if not _batch or _client is None:
        return

    # Atomically grab the current batch and reset.
    events = _batch.copy()
    _batch.clear()

    try:
        resp = await _client.post(
            "/api/public/ingestion",
            json={"batch": events},
        )
        if resp.status_code != 207:
            logger.warning(
                "Langfuse ingestion returned HTTP {} — body: {}",
                resp.status_code,
                resp.text[:500],
            )
        else:
            body = resp.json()
            errors = body.get("errors", [])
            if errors:
                logger.warning("Langfuse ingestion partial errors: {}", errors)
    except Exception:
        logger.opt(exception=True).warning("Langfuse flush failed")


# ── Public API ────────────────────────────────────────────────────────


def start_trace(
    *,
    name: str,
    metadata: dict[str, Any] | None = None,
    tags: list[str] | None = None,
    input: Any | None = None,
    session_id: str | None = None,
    user_id: str | None = None,
) -> TraceContext | None:
    """Create a new Langfuse trace and return a :class:`TraceContext`."""
    if _client is None:
        return None

    trace_id = _uuid()
    ts = _now()

    body: dict[str, Any] = {"id": trace_id, "name": name, "timestamp": ts}
    if metadata:
        body["metadata"] = metadata
    if tags:
        body["tags"] = tags
    if input is not None:
        body["input"] = input
    if session_id:
        body["sessionId"] = session_id
    if user_id:
        body["userId"] = user_id

    _enqueue({"id": _uuid(), "timestamp": ts, "type": "trace-create", "body": body})
    return TraceContext(trace_id=trace_id, name=name, start_time=ts)


def start_span(
    *,
    trace: TraceContext,
    name: str,
    metadata: dict[str, Any] | None = None,
    parent_span_id: str | None = None,
) -> SpanContext | None:
    """Create a new span within *trace*."""
    if _client is None:
        return None

    span_id = _uuid()
    ts = _now()

    body: dict[str, Any] = {
        "id": span_id,
        "traceId": trace.trace_id,
        "name": name,
        "startTime": ts,
    }
    if metadata:
        body["metadata"] = metadata
    if parent_span_id:
        body["parentObservationId"] = parent_span_id

    _enqueue({"id": _uuid(), "timestamp": ts, "type": "span-create", "body": body})
    return SpanContext(span_id=span_id, trace_id=trace.trace_id, name=name, start_time=ts)


def end_span(
    span: SpanContext | None,
    *,
    output: Any | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Close *span* by sending a ``span-update`` event."""
    if _client is None or span is None:
        return

    ts = _now()
    body: dict[str, Any] = {
        "id": span.span_id,
        "traceId": span.trace_id,
        "endTime": ts,
    }
    if output is not None:
        body["output"] = output
    if metadata:
        body["metadata"] = metadata

    _enqueue({"id": _uuid(), "timestamp": ts, "type": "span-update", "body": body})


def log_generation(
    *,
    parent: TraceContext | SpanContext | None,
    name: str,
    model: str,
    input: Any,
    output: Any,
    usage: dict[str, int],
    metadata: dict[str, Any] | None = None,
    model_parameters: dict[str, Any] | None = None,
) -> None:
    """Record an LLM generation event."""
    if _client is None or parent is None:
        return

    ts = _now()
    trace_id = parent.trace_id if isinstance(parent, SpanContext) else parent.trace_id

    body: dict[str, Any] = {
        "id": _uuid(),
        "traceId": trace_id,
        "name": name,
        "model": model,
        "startTime": ts,
        "endTime": ts,
        "input": input,
        "output": output,
        "usageDetails": {
            "input": usage.get("input", 0),
            "output": usage.get("output", 0),
            "total": usage.get("total", 0),
        },
    }

    # Link to parent span if applicable.
    if isinstance(parent, SpanContext):
        body["parentObservationId"] = parent.span_id

    if metadata:
        body["metadata"] = metadata
    if model_parameters:
        body["modelParameters"] = model_parameters

    _enqueue({"id": _uuid(), "timestamp": ts, "type": "generation-create", "body": body})


# ── Shutdown ──────────────────────────────────────────────────────────


async def flush() -> None:
    """Explicitly flush pending events.  Safe to call even if disabled."""
    await _flush()


async def shutdown() -> None:
    """Flush remaining events and close the HTTP client."""
    global _client, _flush_task  # noqa: PLW0603

    if _flush_task is not None:
        _flush_task.cancel()
        try:
            await _flush_task
        except asyncio.CancelledError:
            pass
        _flush_task = None

    await _flush()

    if _client is not None:
        await _client.aclose()
        _client = None
        logger.info("Langfuse tracing shut down")


def reset() -> None:
    """Reset module state (for tests).  Does NOT close the HTTP client."""
    global _client, _flush_task  # noqa: PLW0603
    _batch.clear()
    _client = None
    if _flush_task is not None:
        _flush_task.cancel()
        _flush_task = None
