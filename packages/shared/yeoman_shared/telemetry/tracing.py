"""Langfuse v4 tracing through the official OpenTelemetry-based SDK.

The public helpers keep Yeoman's existing tracing boundary small while the
SDK owns observation export, buffering, retries, and shutdown.  Every helper
is a safe no-op when Langfuse project keys are not configured.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable

from loguru import logger

# The imports stay lazy so an unconfigured runtime does not import the SDK or
# start its OpenTelemetry machinery.
_client: Any | None = None
_propagate_attributes: Callable[..., Any] | None = None


def _load_sdk() -> tuple[type[Any], Callable[..., Any]]:
    """Load the SDK lazily; tests replace this seam with an in-memory fake."""
    from langfuse import Langfuse, propagate_attributes

    return Langfuse, propagate_attributes


# ── Dataclasses ───────────────────────────────────────────────────────


@dataclass
class TraceContext:
    """Opaque handle for one v4 root observation and its propagation scope."""

    trace_id: str
    name: str
    start_time: str
    observation: Any = None
    _observation_context: Any = None
    _propagation_context: Any = None
    children: list["SpanContext"] = field(default_factory=list)
    _children_by_id: dict[str, "SpanContext"] = field(default_factory=dict)
    _ended: bool = False


@dataclass
class SpanContext:
    """Opaque handle for one child observation."""

    span_id: str
    trace_id: str
    name: str
    start_time: str
    observation: Any = None
    trace: TraceContext | None = None
    _ended: bool = False


# ── Initialisation ────────────────────────────────────────────────────


def init(*, flush_interval: float = 5.0, batch_size: int = 50) -> bool:
    """Bootstrap the Langfuse v4 client from environment variables."""
    global _client, _propagate_attributes  # noqa: PLW0603

    if _client is not None:
        return True

    secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "")
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    tracing_enabled = os.environ.get("LANGFUSE_TRACING_ENABLED", "true").lower() != "false"
    if not secret_key or not public_key or not tracing_enabled:
        logger.debug("Langfuse tracing disabled: project keys or tracing flag missing")
        return False

    base_url = os.environ.get("LANGFUSE_BASE_URL", "https://cloud.langfuse.com").rstrip("/")
    try:
        langfuse_class, propagate = _load_sdk()
        _client = langfuse_class(
            public_key=public_key,
            secret_key=secret_key,
            base_url=base_url,
            flush_at=batch_size,
            flush_interval=flush_interval,
            additional_headers={"x-langfuse-ingestion-version": "4"},
        )
        _propagate_attributes = propagate
    except Exception:
        _client = None
        _propagate_attributes = None
        logger.opt(exception=True).warning("Langfuse v4 tracing initialization failed")
        return False

    logger.info("Langfuse v4 tracing enabled (base_url={})", base_url)
    return True


# ── Helpers ───────────────────────────────────────────────────────────


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _stringify_metadata(metadata: dict[str, Any] | None) -> dict[str, str] | None:
    """Make metadata compatible with v4's filterable string attributes."""
    if not metadata:
        return None

    result: dict[str, str] = {}
    for key, value in metadata.items():
        if value is None:
            continue
        if isinstance(value, str):
            rendered = value
        else:
            rendered = json.dumps(value, ensure_ascii=False, default=str)
        result[str(key)] = rendered[:200]
    return result or None


def _model_parameters(parameters: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalize provider-specific values before passing them to the SDK."""
    if not parameters:
        return None

    result: dict[str, Any] = {}
    for key, value in parameters.items():
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            result[str(key)] = value
        elif isinstance(value, list) and all(isinstance(item, str) for item in value):
            result[str(key)] = value
        else:
            result[str(key)] = json.dumps(value, ensure_ascii=False, default=str)
    return result or None


def _usage_details(usage: dict[str, Any] | None) -> dict[str, int] | None:
    """Map Yeoman's provider usage shape to Langfuse v4 usage names."""
    if usage is None:
        return None

    def _as_int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    prompt_tokens = _as_int(usage.get("prompt_tokens", usage.get("input", 0)))
    completion_tokens = _as_int(usage.get("completion_tokens", usage.get("output", 0)))
    total_tokens = _as_int(usage.get("total_tokens", usage.get("total", prompt_tokens + completion_tokens)))
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }


def _observation_for(parent: TraceContext | SpanContext | None) -> Any | None:
    if parent is None:
        return None
    return parent.observation


def _trace_for(parent: TraceContext | SpanContext | None) -> TraceContext | None:
    if isinstance(parent, TraceContext):
        return parent
    return parent.trace if isinstance(parent, SpanContext) else None


def _close_context(context: Any) -> None:
    if context is not None:
        context.__exit__(None, None, None)


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
    """Create a v4 agent observation and return a :class:`TraceContext`."""
    if _client is None or _propagate_attributes is None:
        return None

    propagation_context: Any = None
    observation_context: Any = None
    try:
        propagation_kwargs: dict[str, Any] = {
            "trace_name": name,
            "metadata": _stringify_metadata(metadata),
            "tags": tags,
        }
        if session_id:
            propagation_kwargs["session_id"] = session_id
        if user_id:
            propagation_kwargs["user_id"] = user_id
        propagation_kwargs = {
            key: value for key, value in propagation_kwargs.items() if value is not None
        }

        propagation_context = _propagate_attributes(**propagation_kwargs)
        propagation_context.__enter__()
        observation_context = _client.start_as_current_observation(
            name=name,
            as_type="agent",
            input=input,
        )
        observation = observation_context.__enter__()
        return TraceContext(
            trace_id=str(observation.trace_id),
            name=name,
            start_time=_now(),
            observation=observation,
            _observation_context=observation_context,
            _propagation_context=propagation_context,
        )
    except Exception:
        if observation_context is not None:
            try:
                _close_context(observation_context)
            except Exception:
                logger.opt(exception=True).debug("Langfuse root observation cleanup failed")
        if propagation_context is not None:
            try:
                _close_context(propagation_context)
            except Exception:
                logger.opt(exception=True).debug("Langfuse propagation cleanup failed")
        logger.opt(exception=True).warning("Langfuse root observation creation failed")
        return None


def start_span(
    *,
    trace: TraceContext,
    name: str,
    metadata: dict[str, Any] | None = None,
    parent_span_id: str | None = None,
) -> SpanContext | None:
    """Create a child observation within *trace*."""
    if _client is None or trace is None or trace._ended or trace.observation is None:
        return None

    parent = trace._children_by_id.get(parent_span_id) if parent_span_id else None
    parent_observation = parent.observation if parent is not None else trace.observation
    if parent is not None and parent._ended:
        return None

    as_type = "tool" if name.startswith("tool/") else "span"
    try:
        observation = parent_observation.start_observation(
            name=name,
            as_type=as_type,
            metadata=_stringify_metadata(metadata),
        )
        span = SpanContext(
            span_id=str(observation.id),
            trace_id=str(observation.trace_id),
            name=name,
            start_time=_now(),
            observation=observation,
            trace=trace,
        )
        trace.children.append(span)
        trace._children_by_id[span.span_id] = span
        return span
    except Exception:
        logger.opt(exception=True).warning("Langfuse child observation creation failed: {}", name)
        return None


def start_generation(
    *,
    parent: TraceContext | SpanContext | None,
    name: str,
    model: str,
    input: Any,
    model_parameters: dict[str, Any] | None = None,
) -> SpanContext | None:
    """Start a v4 generation observation under *parent*."""
    trace = _trace_for(parent)
    parent_observation = _observation_for(parent)
    if _client is None or trace is None or trace._ended or parent_observation is None:
        return None

    try:
        observation = parent_observation.start_observation(
            name=name,
            as_type="generation",
            input=input,
            model=model,
            model_parameters=_model_parameters(model_parameters),
        )
        generation = SpanContext(
            span_id=str(observation.id),
            trace_id=str(observation.trace_id),
            name=name,
            start_time=_now(),
            observation=observation,
            trace=trace,
        )
        trace.children.append(generation)
        trace._children_by_id[generation.span_id] = generation
        return generation
    except Exception:
        logger.opt(exception=True).warning("Langfuse generation observation creation failed: {}", name)
        return None


def end_span(
    span: TraceContext | SpanContext | None,
    *,
    output: Any | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Update and close an observation, or the root propagation scope."""
    if _client is None or span is None or span._ended:
        return

    observation = span.observation
    try:
        update_kwargs: dict[str, Any] = {}
        if output is not None:
            update_kwargs["output"] = output
        if metadata:
            update_kwargs["metadata"] = _stringify_metadata(metadata)
        if update_kwargs and observation is not None:
            observation.update(**update_kwargs)
    except Exception:
        logger.opt(exception=True).warning("Langfuse observation update failed: {}", span.name)
    finally:
        try:
            if isinstance(span, TraceContext):
                if span._observation_context is not None:
                    _close_context(span._observation_context)
                elif observation is not None:
                    observation.end()
                try:
                    _close_context(span._propagation_context)
                except Exception:
                    logger.opt(exception=True).debug("Langfuse propagation close failed")
            elif observation is not None:
                observation.end()
        except Exception:
            logger.opt(exception=True).warning("Langfuse observation close failed: {}", span.name)
        finally:
            span._ended = True


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
    """Record a completed v4 generation observation."""
    generation = start_generation(
        parent=parent,
        name=name,
        model=model,
        input=input,
        model_parameters=model_parameters,
    )
    end_generation(generation, output=output, usage=usage, metadata=metadata)


def end_generation(
    generation: SpanContext | None,
    *,
    output: Any | None = None,
    usage: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    level: str | None = None,
    status_message: str | None = None,
) -> None:
    """Complete a generation with output, usage, and optional error status."""
    if _client is None or generation is None or generation._ended:
        return

    update_kwargs: dict[str, Any] = {}
    if output is not None:
        update_kwargs["output"] = output
    usage_details = _usage_details(usage)
    if usage_details is not None:
        update_kwargs["usage_details"] = usage_details
    if metadata:
        update_kwargs["metadata"] = _stringify_metadata(metadata)
    if level is not None:
        update_kwargs["level"] = level
    if status_message is not None:
        update_kwargs["status_message"] = status_message

    try:
        if update_kwargs and generation.observation is not None:
            generation.observation.update(**update_kwargs)
    except Exception:
        logger.opt(exception=True).warning("Langfuse generation update failed: {}", generation.name)
    finally:
        try:
            if generation.observation is not None:
                generation.observation.end()
        except Exception:
            logger.opt(exception=True).warning("Langfuse generation close failed: {}", generation.name)
        finally:
            generation._ended = True


# ── Shutdown ──────────────────────────────────────────────────────────


async def flush() -> None:
    """Flush the SDK's pending observations.  Safe to call if disabled."""
    if _client is None:
        return
    try:
        await asyncio.to_thread(_client.flush)
    except Exception:
        logger.opt(exception=True).warning("Langfuse flush failed")


async def shutdown() -> None:
    """Flush remaining observations and close the SDK client."""
    global _client, _propagate_attributes  # noqa: PLW0603

    client = _client
    if client is None:
        return
    try:
        await asyncio.to_thread(client.shutdown)
    except Exception:
        logger.opt(exception=True).warning("Langfuse shutdown failed")
    finally:
        _client = None
        _propagate_attributes = None
        logger.info("Langfuse tracing shut down")


def reset() -> None:
    """Reset module state for tests without calling SDK shutdown."""
    global _client, _propagate_attributes  # noqa: PLW0603
    _client = None
    _propagate_attributes = None
