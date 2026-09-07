# Langfuse Tracing Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add nested Langfuse traces to yeoman's agent loop so each message shows LLM calls, tool executions, token usage, and latency in the Langfuse Cloud dashboard.

**Architecture:** A new `yeoman/telemetry/tracing.py` module initializes a Langfuse client when `LANGFUSE_SECRET_KEY` is present in env; otherwise all helpers are no-ops. The module is imported in `responder_llm.py` and `litellm_provider.py` to wrap the agent loop, LLM calls, and tool calls with nested traces/spans/generations.

**Tech Stack:** `langfuse` Python SDK v3+, LiteLLM (existing), Python 3.14

---

### Task 1: Add langfuse dependency

**Files:**
- Modify: `pyproject.toml:18-36`

**Step 1: Add langfuse to dependencies**

In `pyproject.toml`, add `langfuse>=3.0.0` to the `dependencies` list, after the existing `litellm` entry:

```toml
dependencies = [
    "typer>=0.23.1",
    "litellm>=1.81.12",
    "langfuse>=3.0.0",
    ...
]
```

**Step 2: Sync dependencies**

Run: `cd ~/Documents/yeoman && uv sync`
Expected: langfuse installed successfully, no errors.

**Step 3: Verify import**

Run: `cd ~/Documents/yeoman && uv run python -c "import langfuse; print(langfuse.__version__)"`
Expected: prints version 3.x.x

**Step 4: Commit**

```bash
cd ~/Documents/yeoman
git add pyproject.toml uv.lock
git commit -m "deps: add langfuse SDK for agent tracing"
```

---

### Task 2: Create tracing module with no-op safety

**Files:**
- Create: `yeoman/telemetry/tracing.py`
- Test: `tests/test_tracing.py`

**Step 1: Write the failing test**

Create `tests/test_tracing.py`:

```python
"""Tests for Langfuse tracing helpers."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch


class TestTracingInit:
    """Tracing module initializes from env or stays no-op."""

    def test_noop_when_no_env(self, monkeypatch: object) -> None:
        """Without LANGFUSE_SECRET_KEY, get_client() returns None."""
        import importlib
        import os

        monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)  # type: ignore[attr-defined]

        import yeoman.telemetry.tracing as mod
        importlib.reload(mod)

        assert mod.get_client() is None

    def test_client_when_env_set(self, monkeypatch: object) -> None:
        """With LANGFUSE_SECRET_KEY, get_client() returns a Langfuse instance."""
        import importlib

        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")  # type: ignore[attr-defined]
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")  # type: ignore[attr-defined]

        import yeoman.telemetry.tracing as mod
        importlib.reload(mod)

        client = mod.get_client()
        assert client is not None


class TestTraceHelpers:
    """Trace/span/generation helper functions."""

    def test_start_trace_noop_returns_none(self) -> None:
        from yeoman.telemetry.tracing import start_trace

        # With no client, returns None
        result = start_trace(name="test", metadata={})
        assert result is None

    def test_start_span_noop_returns_none(self) -> None:
        from yeoman.telemetry.tracing import start_span

        result = start_span(trace=None, name="test")
        assert result is None

    def test_end_span_noop_no_error(self) -> None:
        from yeoman.telemetry.tracing import end_span

        # Should not raise
        end_span(None)

    def test_log_generation_noop_no_error(self) -> None:
        from yeoman.telemetry.tracing import log_generation

        # Should not raise
        log_generation(parent=None, name="test", model="test", input={}, output="", usage={})
```

**Step 2: Run test to verify it fails**

Run: `cd ~/Documents/yeoman && uv run pytest tests/test_tracing.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'yeoman.telemetry.tracing'`

**Step 3: Write the tracing module**

Create `yeoman/telemetry/tracing.py`:

```python
"""Langfuse tracing helpers.

Auto-enables when LANGFUSE_SECRET_KEY is present in environment.
All helpers are no-ops when the client is not initialized.
"""

from __future__ import annotations

import os
from typing import Any

from loguru import logger

_client: Any = None


def _init() -> None:
    """Initialize Langfuse client from environment if keys are present."""
    global _client
    if not os.environ.get("LANGFUSE_SECRET_KEY"):
        return
    try:
        from langfuse import Langfuse

        _client = Langfuse()
        logger.info("langfuse tracing enabled")
    except Exception as e:
        logger.warning("langfuse init failed: {}", e)
        _client = None


_init()


def get_client() -> Any:
    """Return the Langfuse client, or None if tracing is disabled."""
    return _client


def start_trace(
    *,
    name: str,
    metadata: dict[str, Any],
    tags: list[str] | None = None,
) -> Any:
    """Create a root trace. Returns the trace object, or None if disabled."""
    if _client is None:
        return None
    try:
        return _client.trace(name=name, metadata=metadata, tags=tags or [])
    except Exception as e:
        logger.debug("langfuse start_trace failed: {}", e)
        return None


def start_span(
    *,
    trace: Any,
    name: str,
    metadata: dict[str, Any] | None = None,
) -> Any:
    """Create a child span under a trace or span. Returns the span, or None."""
    if trace is None:
        return None
    try:
        return trace.span(name=name, metadata=metadata or {})
    except Exception as e:
        logger.debug("langfuse start_span failed: {}", e)
        return None


def end_span(span: Any, *, metadata: dict[str, Any] | None = None) -> None:
    """End a span. No-op if span is None."""
    if span is None:
        return
    try:
        span.end(metadata=metadata)
    except Exception as e:
        logger.debug("langfuse end_span failed: {}", e)


def log_generation(
    *,
    parent: Any,
    name: str,
    model: str,
    input: Any,
    output: str | None,
    usage: dict[str, int],
    metadata: dict[str, Any] | None = None,
) -> None:
    """Log an LLM generation under a trace or span. No-op if parent is None."""
    if parent is None:
        return
    try:
        parent.generation(
            name=name,
            model=model,
            input=input,
            output=output,
            usage=usage,
            metadata=metadata or {},
        )
    except Exception as e:
        logger.debug("langfuse log_generation failed: {}", e)
```

**Step 4: Run tests to verify they pass**

Run: `cd ~/Documents/yeoman && uv run pytest tests/test_tracing.py -v`
Expected: all 6 tests PASS

**Step 5: Commit**

```bash
cd ~/Documents/yeoman
git add yeoman/telemetry/tracing.py tests/test_tracing.py
git commit -m "feat(telemetry): add langfuse tracing module with no-op safety"
```

---

### Task 3: Instrument `_generate()` — root trace

**Files:**
- Modify: `yeoman/adapters/responder_llm.py:816-998`

**Step 1: Add import at the top of responder_llm.py**

After the existing imports (around line 16), add:

```python
from yeoman.telemetry import tracing as lf
```

**Step 2: Wrap `_generate()` with root trace**

At the start of `_generate()` (line 836, after the method signature), create the trace:

```python
    trace = lf.start_trace(
        name="generate",
        metadata={
            "channel": channel,
            "chat_id": chat_id,
            "session_key": session_key,
            "model": self._model_for_profile(model_profile) or self.model,
        },
        tags=[channel],
    )
```

Store `self._current_trace = trace` right after creating it. Before returning `final_content` at the end of `_generate()`, end the trace:

```python
    lf.end_span(trace)
    self._current_trace = None
```

**Step 3: Run existing tests to verify no regression**

Run: `cd ~/Documents/yeoman && uv run pytest tests/ -v --timeout=30 -x`
Expected: all existing tests still pass

**Step 4: Commit**

```bash
cd ~/Documents/yeoman
git add yeoman/adapters/responder_llm.py
git commit -m "feat(tracing): instrument _generate() as root langfuse trace"
```

---

### Task 4: Instrument `_chat_loop()` — iteration spans

**Files:**
- Modify: `yeoman/adapters/responder_llm.py:475-573`

**Step 1: Pass trace into `_chat_loop()`**

Add a `trace: Any = None` parameter to `_chat_loop()`:

```python
    async def _chat_loop(
        self,
        *,
        messages: list[dict[str, Any]],
        allowed_tools: set[str],
        security_context: dict[str, object] | None = None,
        is_owner: bool = False,
        model: str | None = None,
        trace: Any = None,
    ) -> str:
```

**Step 2: Add iteration span inside the while loop**

At the top of the while loop body (after `iteration += 1`), open an iteration span:

```python
            iter_span = lf.start_span(
                trace=trace,
                name=f"iteration-{iteration}",
            )
```

Before each `continue` and before `break`, end the span:

```python
            lf.end_span(iter_span)
```

Also end the span in the `else` clause (max iterations reached):

```python
        else:
            lf.end_span(iter_span)
            return "⚙️❓"
```

**Step 3: Store `iter_span` on self for provider to access**

Before calling `self.provider.chat()`, set:

```python
            self._current_iter_span = iter_span
```

After the response, clear it:

```python
            self._current_iter_span = None
```

**Step 4: Pass trace from `_generate()` call site**

In `_generate()` where `_chat_loop()` is called (~line 939), add:

```python
                final_content = await self._chat_loop(
                    messages=messages,
                    allowed_tools=allowed_tools,
                    security_context={...},
                    is_owner=is_owner,
                    model=self._model_for_profile(model_profile),
                    trace=trace,
                )
```

**Step 5: Run tests**

Run: `cd ~/Documents/yeoman && uv run pytest tests/ -v --timeout=30 -x`
Expected: all tests pass

**Step 6: Commit**

```bash
cd ~/Documents/yeoman
git add yeoman/adapters/responder_llm.py
git commit -m "feat(tracing): add per-iteration spans in chat loop"
```

---

### Task 5: Instrument `provider.chat()` — generation spans

**Files:**
- Modify: `yeoman/providers/litellm_provider.py:101-154`

**Step 1: Add import**

At the top of `litellm_provider.py`:

```python
from yeoman.telemetry import tracing as lf
```

**Step 2: Add generation logging after successful response**

In the `chat()` method, after `response = await acompletion(**kwargs)` and `return self._parse_response(response)`, restructure to capture the parsed result and log it:

```python
        try:
            response = await acompletion(**kwargs)
            parsed = self._parse_response(response)
            lf.log_generation(
                parent=getattr(self, "_current_span", None),
                name="llm",
                model=model,
                input={"message_count": len(messages), "has_tools": bool(tools)},
                output=parsed.content,
                usage=parsed.usage,
            )
            return parsed
        except Exception as e:
            ...
```

**Step 3: Wire the span from responder**

In `_chat_loop()` in `responder_llm.py`, before calling `self.provider.chat()`, set the span on the provider so it can access it:

```python
            self.provider._current_span = iter_span
```

After the call, clear it:

```python
            self.provider._current_span = None
```

**Step 4: Run tests**

Run: `cd ~/Documents/yeoman && uv run pytest tests/ -v --timeout=30 -x`
Expected: all tests pass

**Step 5: Commit**

```bash
cd ~/Documents/yeoman
git add yeoman/providers/litellm_provider.py yeoman/adapters/responder_llm.py
git commit -m "feat(tracing): log LLM generations with token usage"
```

---

### Task 6: Instrument `_execute_tool()` — tool spans

**Files:**
- Modify: `yeoman/adapters/responder_llm.py:462-473`

**Step 1: Add tracing to `_execute_tool()`**

Wrap the tool execution with a span. The tool call loop in `_chat_loop()` already has access to `iter_span`. Before calling `_execute_tool()`, open a tool span; after, end it:

In `_chat_loop()`, around lines 542-552 where `_execute_tool()` is called, wrap each call:

```python
                        tool_span = lf.start_span(
                            trace=iter_span,
                            name=f"tool/{tool_call.name}",
                            metadata={"arguments": args_preview[:500]},
                        )
                        result = await self._execute_tool(
                            tool_call.name,
                            tool_call.arguments,
                            is_owner=is_owner,
                        )
                        lf.end_span(tool_span, metadata={
                            "result": result[:500] if result else "",
                        })
```

Apply the same wrapping to both code paths (with and without security check). There are two `_execute_tool()` call sites in the loop — wrap both.

**Step 2: Run tests**

Run: `cd ~/Documents/yeoman && uv run pytest tests/ -v --timeout=30 -x`
Expected: all tests pass

**Step 3: Commit**

```bash
cd ~/Documents/yeoman
git add yeoman/adapters/responder_llm.py
git commit -m "feat(tracing): add tool execution spans"
```

---

### Task 7: Integration smoke test

**Files:**
- Create: `tests/test_tracing_integration.py`

**Step 1: Write integration test**

This test verifies the full trace structure using a mock Langfuse client:

```python
"""Integration test for langfuse tracing through the agent loop."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from yeoman.telemetry import tracing as lf


class TestTracingIntegration:
    """Verify trace structure: trace → iteration → generation + tool spans."""

    def test_trace_structure_with_mock_client(self) -> None:
        """Full trace tree from start_trace through spans and generation."""
        mock_client = MagicMock()
        mock_trace = MagicMock()
        mock_span = MagicMock()
        mock_client.trace.return_value = mock_trace
        mock_trace.span.return_value = mock_span
        mock_span.span.return_value = MagicMock()

        with patch.object(lf, "_client", mock_client):
            trace = lf.start_trace(name="generate", metadata={"channel": "whatsapp"})
            assert trace is mock_trace

            iter_span = lf.start_span(trace=trace, name="iteration-1")
            assert iter_span is mock_span

            lf.log_generation(
                parent=iter_span,
                name="llm",
                model="claude-sonnet-4-5",
                input={"message_count": 5},
                output="Hello!",
                usage={"prompt_tokens": 100, "completion_tokens": 10},
            )

            tool_span = lf.start_span(trace=iter_span, name="tool/web_search")
            lf.end_span(tool_span, metadata={"result": "search results"})

            lf.end_span(iter_span)
            lf.end_span(trace)

        mock_client.trace.assert_called_once()
        mock_trace.span.assert_called()
        mock_span.generation.assert_called_once()

    def test_all_noop_when_disabled(self) -> None:
        """When client is None, all helpers are silent no-ops."""
        with patch.object(lf, "_client", None):
            trace = lf.start_trace(name="test", metadata={})
            assert trace is None

            span = lf.start_span(trace=None, name="test")
            assert span is None

            # Should not raise
            lf.end_span(None)
            lf.log_generation(
                parent=None, name="t", model="t",
                input={}, output="", usage={},
            )
```

**Step 2: Run all tests**

Run: `cd ~/Documents/yeoman && uv run pytest tests/ -v --timeout=30`
Expected: all tests pass

**Step 3: Commit**

```bash
cd ~/Documents/yeoman
git add tests/test_tracing_integration.py
git commit -m "test: add langfuse tracing integration test"
```

---

### Task 8: Deploy and verify

**Step 1: Sync installed package**

Run: `cd ~/Documents/yeoman && uv sync`

**Step 2: Restart gateway**

Run: `yeoman gateway restart`

**Step 3: Send a test message to the bot**

Send a message through any active channel (Telegram or WhatsApp).

**Step 4: Check Langfuse dashboard**

Open https://cloud.langfuse.com and verify:
- A trace appears named "generate"
- It contains nested iteration spans
- Each iteration has an "llm" generation with token counts
- Tool calls (if any) appear as child spans

**Step 5: Commit all remaining changes (if any)**

```bash
cd ~/Documents/yeoman
git add -A
git commit -m "feat: langfuse tracing for agent loop"
```
