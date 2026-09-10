"""Unit tests for the Langfuse v4 tracing boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar

import pytest
from yeoman_shared.telemetry import tracing


@dataclass
class FakeObservation:
    """Small SDK-shaped observation used without network access."""

    trace_id: str
    id: str
    name: str
    as_type: str
    parent: FakeObservation | None = None
    input: Any = None
    metadata: Any = None
    updates: list[dict[str, Any]] = field(default_factory=list)
    end_count: int = 0
    _next_id: ClassVar[int] = 0

    def start_observation(self, *, name: str, as_type: str = "span", **kwargs: Any) -> FakeObservation:
        FakeObservation._next_id += 1
        return FakeObservation(
            trace_id=self.trace_id,
            id=f"obs-{FakeObservation._next_id}",
            name=name,
            as_type=as_type,
            parent=self,
            input=kwargs.get("input"),
            metadata=kwargs.get("metadata"),
        )

    def update(self, **kwargs: Any) -> FakeObservation:
        self.updates.append(kwargs)
        if "input" in kwargs:
            self.input = kwargs["input"]
        if "metadata" in kwargs:
            self.metadata = kwargs["metadata"]
        return self

    def end(self) -> FakeObservation:
        self.end_count += 1
        return self


class FakeRootContext:
    def __init__(self, observation: FakeObservation) -> None:
        self.observation = observation

    def __enter__(self) -> FakeObservation:
        return self.observation

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.observation.end()


class FakePropagationContext:
    def __init__(self, calls: list[dict[str, Any]], kwargs: dict[str, Any]) -> None:
        self.calls = calls
        self.kwargs = kwargs

    def __enter__(self) -> FakePropagationContext:
        self.calls.append(self.kwargs)
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


class FakeLangfuse:
    instances: list[FakeLangfuse] = []

    def __init__(self, **kwargs: Any) -> None:
        self.init_kwargs = kwargs
        self.propagation_calls: list[dict[str, Any]] = []
        self.root: FakeObservation | None = None
        self.flush_count = 0
        self.shutdown_count = 0
        FakeLangfuse.instances.append(self)

    def start_as_current_observation(self, **kwargs: Any) -> FakeRootContext:
        FakeObservation._next_id += 1
        self.root = FakeObservation(
            trace_id=f"trace-{FakeObservation._next_id}",
            id=f"obs-{FakeObservation._next_id}",
            name=kwargs["name"],
            as_type=kwargs.get("as_type", "span"),
            input=kwargs.get("input"),
            metadata=kwargs.get("metadata"),
        )
        return FakeRootContext(self.root)

    def flush(self) -> None:
        self.flush_count += 1

    def shutdown(self) -> None:
        self.shutdown_count += 1


def fake_propagate_attributes(**kwargs: Any) -> FakePropagationContext:
    client = FakeLangfuse.instances[-1]
    return FakePropagationContext(client.propagation_calls, kwargs)


@pytest.fixture(autouse=True)
def _clean_module_state(monkeypatch: pytest.MonkeyPatch):
    tracing.reset()
    FakeLangfuse.instances.clear()
    FakeObservation._next_id = 0
    monkeypatch.setattr(tracing, "_load_sdk", lambda: (FakeLangfuse, fake_propagate_attributes))
    yield
    tracing.reset()
    FakeLangfuse.instances.clear()


def _init_tracing(monkeypatch: pytest.MonkeyPatch) -> FakeLangfuse:
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test-secret")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test-public")
    assert tracing.init() is True
    client = FakeLangfuse.instances[-1]
    assert client.init_kwargs["additional_headers"] == {"x-langfuse-ingestion-version": "4"}
    return client


class TestNoOpWhenDisabled:
    def test_init_returns_false_without_project_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
        monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)

        assert tracing.init() is False
        assert tracing._client is None

    def test_observation_helpers_are_safe_noops(self) -> None:
        assert tracing.start_trace(name="test") is None
        tracing.end_span(None)
        tracing.log_generation(
            parent=None,
            name="gen",
            model="gpt-4",
            input="hi",
            output="bye",
            usage={"input": 1, "output": 2, "total": 3},
        )


class TestV4Observations:
    def test_start_trace_creates_agent_root_and_propagates_context(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _init_tracing(monkeypatch)

        trace = tracing.start_trace(
            name="generate",
            metadata={"channel": "whatsapp", "attempt": 1},
            tags=["whatsapp"],
            input="hello",
            session_id="session-1",
            user_id="user-1",
        )

        assert trace is not None
        assert client.root is not None
        assert trace.observation is client.root
        assert client.root.as_type == "agent"
        assert client.root.input == "hello"
        assert client.propagation_calls == [
            {
                "trace_name": "generate",
                "user_id": "user-1",
                "session_id": "session-1",
                "metadata": {"channel": "whatsapp", "attempt": "1"},
                "tags": ["whatsapp"],
            }
        ]

    def test_children_use_v4_observation_types_and_parentage(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_tracing(monkeypatch)
        trace = tracing.start_trace(name="generate")
        assert trace is not None

        iteration = tracing.start_span(trace=trace, name="iteration-1")
        assert iteration is not None
        tool = tracing.start_span(
            trace=trace,
            name="tool/web_search",
            metadata={"arguments": "{}"},
            parent_span_id=iteration.span_id,
        )

        assert tool is not None
        assert iteration.observation.as_type == "span"
        assert tool.observation.as_type == "tool"
        assert tool.observation.parent is iteration.observation

        tracing.end_span(tool, output={"result": "ok"})
        tracing.end_span(iteration)
        tracing.end_span(trace, output="answer")

        assert tool.observation.end_count == 1
        assert iteration.observation.end_count == 1
        assert trace.observation.end_count == 1

    def test_log_generation_creates_completed_generation_with_v4_usage(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_tracing(monkeypatch)
        trace = tracing.start_trace(name="generate")
        assert trace is not None

        tracing.log_generation(
            parent=trace,
            name="llm",
            model="openai/gpt-5",
            input={"message_count": 2},
            output="hello",
            usage={"input": 10, "output": 20, "total": 30},
            metadata={"channel": "whatsapp"},
            model_parameters={"temperature": 0.7},
        )

        generation = trace.children[-1]
        assert generation.observation.as_type == "generation"
        assert generation.observation.end_count == 1
        update = generation.observation.updates[-1]
        assert update["output"] == "hello"
        assert update["usage_details"] == {
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "total_tokens": 30,
        }


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_flush_and_shutdown_delegate_to_sdk(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _init_tracing(monkeypatch)

        await tracing.flush()
        await tracing.shutdown()

        assert client.flush_count == 1
        assert client.shutdown_count == 1
        assert tracing._client is None
