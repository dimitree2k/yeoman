from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from yeoman_gateway.a2a.client import A2AWorker, A2AWorkerResult
from yeoman_gateway.a2a.registry import A2AWorkerRegistry
from yeoman_gateway.agent.tools.a2a import A2ADelegateTool
from yeoman_gateway.agent.tools.a2a_research import A2AResearchStore, PendingResearch
from yeoman_gateway.processing.dispatch import CURRENT_TURN
from yeoman_gateway.processing.store import ProcessingStore
from yeoman_gateway.processing.tool_context import (
    ToolInvocationContext,
    current_tool_context,
    reset_tool_context,
    set_tool_context,
)


@pytest.mark.asyncio
async def test_registry_and_delegate_use_structured_skill_input() -> None:
    calls: list[tuple[str, dict[str, object], str | None]] = []

    class FakeClient:
        async def invoke_skill(
            self,
            skill: str,
            input: dict[str, object],
            *,
            context_id: str | None = None,
            reference_task_ids=(),
        ) -> A2AWorkerResult:
            del reference_task_ids
            calls.append((skill, input, context_id))
            return A2AWorkerResult(
                "hermes", "task-3", "ctx-3", "TASK_STATE_COMPLETED", skill, {"results": []}
            )

    registry = A2AWorkerRegistry(
        [A2AWorker(name="hermes", url="http://127.0.0.1:9900")],
        client_factory=lambda worker: FakeClient(),
    )
    tool = A2ADelegateTool(registry)

    assert set(tool.parameters["required"]) == {"worker", "skill", "input"}
    assert "message" not in tool.parameters["properties"]
    assert (
        await tool.execute(worker="hermes", skill="search.web", input={"query": "weather"})
        == '[hermes | search.web | TASK_STATE_COMPLETED | task-3]\n{"results": []}'
    )
    assert calls == [("search.web", {"query": "weather"}, None)]


@pytest.mark.asyncio
async def test_trading_delegation_requests_markdown_output() -> None:
    calls: list[tuple[str, dict[str, object], str | None]] = []

    class FakeClient:
        async def invoke_skill(
            self,
            skill: str,
            input: dict[str, object],
            *,
            context_id: str | None = None,
            reference_task_ids=(),
        ) -> A2AWorkerResult:
            del reference_task_ids
            calls.append((skill, input, context_id))
            return A2AWorkerResult(
                "hermes", "task-markdown", "ctx-markdown", "TASK_STATE_COMPLETED", skill,
                {"report": "# Report", "sources": []},
            )

    tool = A2ADelegateTool(
        A2AWorkerRegistry(
            [A2AWorker(name="hermes", url="http://127.0.0.1:9900")],
            client_factory=lambda _: FakeClient(),
        )
    )

    await tool.execute(
        worker="hermes",
        skill="trading.analyze",
        input={"question": "Analyse KO", "idempotency_key": "ko-1"},
    )

    assert calls == [
        (
            "trading.analyze",
            {"question": "Analyse KO", "idempotency_key": "ko-1", "output_format": "markdown"},
            None,
        )
    ]


@pytest.mark.asyncio
async def test_registry_rejects_unknown_worker() -> None:
    with pytest.raises(KeyError, match="unknown A2A worker 'missing'"):
        await A2AWorkerRegistry([]).invoke_skill("missing", "search.web", {"query": "q"})


@pytest.mark.asyncio
async def test_working_research_is_polled_and_delivered() -> None:
    class Client:
        async def invoke_skill(self, skill, input, *, context_id=None, reference_task_ids=()):
            del input
            return A2AWorkerResult(
                "hermes",
                "research-1",
                context_id or "ctx-1",
                "TASK_STATE_WORKING",
                skill,
                reference_task_ids=tuple(reference_task_ids),
            )

        async def poll_task(self, task_id, *, skill, context_id, reference_task_ids=()):
            assert (task_id, skill, context_id, tuple(reference_task_ids)) == (
                "research-1",
                "research.deep",
                "ctx-1",
                (),
            )
            return A2AWorkerResult(
                "hermes",
                task_id,
                context_id,
                "TASK_STATE_COMPLETED",
                skill,
                {"report": "done", "sources": []},
            )

    class Delivery:
        def __init__(self):
            self.sent = []

        async def send(self, **kwargs):
            self.sent.append(kwargs)

    delivery = Delivery()
    tool = A2ADelegateTool(
        A2AWorkerRegistry(
            [A2AWorker(name="hermes", url="http://127.0.0.1:9900")],
            client_factory=lambda _: Client(),
        ),
        delivery=delivery,
    )
    tool.set_context("whatsapp", "chat@g.us")
    assert "TASK_STATE_WORKING" in await tool.execute(
        worker="hermes", skill="research.deep", input={"question": "q", "idempotency_key": "r1"}
    )
    while tool._background:
        await asyncio.sleep(0)
    assert len(delivery.sent) == 1
    sent = delivery.sent[0]
    assert sent["source"] == "a2a"
    assert sent["operation_ref"] == "a2a-result:"
    assert sent["channel"] == "whatsapp"
    assert sent["chat_id"] == "chat@g.us"
    # A detached result is labelled as a follow-up to the request that started it.
    assert sent["content"].startswith("Nachtrag zur Trading-Recherche zu deiner Frage „q“")
    assert sent["content"].endswith("done")
    assert '{"report"' not in sent["content"], "structured payloads are rendered, not dumped"


@pytest.mark.asyncio
async def test_research_timeout_and_protocol_failures_have_distinct_delivery_codes() -> None:
    from yeoman_gateway.a2a.client import (
        A2APollTimeoutError,
        A2AProtocolError,
        A2ATransportError,
    )

    class Delivery:
        def __init__(self):
            self.sent = []

        async def send(self, **kwargs):
            self.sent.append(kwargs)

    class Registry:
        def __init__(self, error):
            self.error = error

        async def poll_task(self, *args, **kwargs):
            raise self.error

    result = A2AWorkerResult("hermes", "t", "c", "TASK_STATE_WORKING", "research.deep")
    for error, expected in (
        (A2APollTimeoutError("x"), "error=POLL_TIMEOUT retryable=True"),
        (A2AProtocolError("x"), "error=PROTOCOL_FAILURE retryable=False"),
        (A2ATransportError("x"), "error=TRANSPORT_FAILURE retryable=True"),
        (RuntimeError("signed private secret"), "error=POLL_FAILURE retryable=False"),
    ):
        delivery = Delivery()
        tool = A2ADelegateTool(Registry(error), delivery=delivery)
        await tool._poll_research(
            "hermes", result.task_id, result.skill, result.context_id, (), "e", "whatsapp", "chat"
        )
        assert delivery.sent[0]["content"].endswith(expected)
        assert delivery.sent[0]["content"].startswith("Nachtrag zur Trading-Recherche")
        assert len(delivery.sent) == 1
        assert "signed private secret" not in delivery.sent[0]["content"]


@pytest.mark.asyncio
async def test_research_poll_timeout_extends_instead_of_failing_long_research() -> None:
    """A poll window expiring must not fail research that is still progressing."""
    from yeoman_gateway.a2a.client import A2APollTimeoutError

    class Delivery:
        def __init__(self):
            self.sent = []

        async def send(self, **kwargs):
            self.sent.append(kwargs)

    class Registry:
        def __init__(self):
            self.calls = 0

        async def poll_task(self, worker, task_id, *, skill, context_id, reference_task_ids=()):
            del worker, task_id, skill, context_id, reference_task_ids
            self.calls += 1
            if self.calls <= 2:
                raise A2APollTimeoutError("window expired")
            return A2AWorkerResult(
                "hermes",
                "task-long",
                "ctx-long",
                "TASK_STATE_COMPLETED",
                "research.deep",
                {"report": "long research finished", "sources": ["https://example.test"]},
            )

    registry = Registry()
    delivery = Delivery()
    tool = A2ADelegateTool(registry, delivery=delivery)

    await tool._poll_research(
        "hermes", "task-long", "research.deep", "ctx-long", (), "effect-1", "whatsapp", "chat"
    )

    assert registry.calls == 3, "the expired windows must be retried, not surfaced"
    assert len(delivery.sent) == 1
    assert "long research finished" in delivery.sent[0]["content"]
    assert "POLL_TIMEOUT" not in delivery.sent[0]["content"]


@pytest.mark.asyncio
async def test_research_poll_timeout_is_reported_after_bounded_extensions() -> None:
    """The extension budget is finite: a task that never reports still ends in timeout."""
    from yeoman_gateway.a2a.client import A2APollTimeoutError

    class Delivery:
        def __init__(self):
            self.sent = []

        async def send(self, **kwargs):
            self.sent.append(kwargs)

    class Registry:
        def __init__(self):
            self.calls = 0

        async def poll_task(self, *args, **kwargs):
            del args, kwargs
            self.calls += 1
            raise A2APollTimeoutError("window expired")

    registry = Registry()
    delivery = Delivery()
    tool = A2ADelegateTool(registry, delivery=delivery, research_poll_extensions=2)

    await tool._poll_research(
        "hermes", "task-stuck", "research.deep", "ctx-stuck", (), "effect-2", "whatsapp", "chat"
    )

    assert registry.calls == 3, "one initial window plus exactly two extensions"
    assert len(delivery.sent) == 1
    assert delivery.sent[0]["content"].endswith("error=POLL_TIMEOUT retryable=True")
    assert delivery.sent[0]["content"].startswith("Nachtrag zur Trading-Recherche")


@pytest.mark.asyncio
async def test_follow_up_header_names_the_request_and_its_age() -> None:
    """A result that arrives after the chat moved on is marked as a follow-up, not suppressed."""
    import time as _time

    from yeoman_gateway.a2a.client import A2AWorkerResult

    class Registry:
        def __init__(self) -> None:
            self.references: tuple[str, ...] = ()

        async def poll_task(self, *args, **kwargs):
            return A2AWorkerResult(
                "hermes",
                "task-late",
                "ctx-late",
                "TASK_STATE_COMPLETED",
                "research.deep",
                {"report": "fertig"},
                reference_task_ids=self.references,
            )

    class Delivery:
        def __init__(self) -> None:
            self.sent: list[dict[str, object]] = []

        async def send(self, **kwargs):
            self.sent.append(kwargs)

    delivery = Delivery()
    tool = A2ADelegateTool(Registry(), delivery=delivery)
    asked = "TradingAgents-Analyse für Microsoft (MSFT) für heute: Kurs, Treiber, Risiken."

    await tool._poll_research(
        "hermes",
        "task-late",
        "research.deep",
        "ctx-late",
        (),
        "effect-late",
        "whatsapp",
        "chat@g.us",
        asked,
        int(_time.time() * 1000) - 12 * 60 * 1000,
    )

    content = str(delivery.sent[0]["content"])
    assert content.startswith("Nachtrag zur Trading-Recherche zu deiner Frage „TradingAgents-Analyse für Microsoft (MSFT)")
    assert "angefragt vor 12 Minuten" in content
    assert content.endswith("fertig"), content
    assert '{"report"' not in content


@pytest.mark.asyncio
async def test_follow_up_header_survives_a_missing_question() -> None:
    """Resumed tasks from an older schema have no stored question: still labelled, never bare."""
    from yeoman_gateway.a2a.client import A2AWorkerResult

    class Registry:
        async def poll_task(self, *args, **kwargs):
            return A2AWorkerResult(
                "hermes", "t", "c", "TASK_STATE_COMPLETED", "research.deep", {"report": "x"}
            )

    class Delivery:
        def __init__(self) -> None:
            self.sent: list[dict[str, object]] = []

        async def send(self, **kwargs):
            self.sent.append(kwargs)

    delivery = Delivery()
    tool = A2ADelegateTool(Registry(), delivery=delivery)
    await tool._poll_research(
        "hermes", "t", "research.deep", "c", (), "e", "whatsapp", "chat@g.us", "", 0
    )

    content = str(delivery.sent[0]["content"])
    assert content.startswith("Nachtrag zur Trading-Recherche (angefragt vor unter einer Minute):")


@pytest.mark.asyncio
async def test_research_poll_extensions_can_be_disabled() -> None:
    from yeoman_gateway.a2a.client import A2APollTimeoutError

    class Delivery:
        async def send(self, **kwargs):
            del kwargs

    class Registry:
        def __init__(self):
            self.calls = 0

        async def poll_task(self, *args, **kwargs):
            del args, kwargs
            self.calls += 1
            raise A2APollTimeoutError("window expired")

    registry = Registry()
    tool = A2ADelegateTool(registry, delivery=Delivery(), research_poll_extensions=0)

    await tool._poll_research(
        "hermes", "task-x", "research.deep", "ctx-x", (), "effect-3", "whatsapp", "chat"
    )

    assert registry.calls == 1


@pytest.mark.asyncio
async def test_research_poll_is_detached_from_closed_turn_and_deleted_after_delivery(
    tmp_path: Path,
) -> None:
    seen_contexts: list[tuple[object, object]] = []

    class Client:
        async def invoke_skill(self, skill, input, *, context_id=None, reference_task_ids=()):
            del input, reference_task_ids
            return A2AWorkerResult(
                "hermes", "research-detached", context_id or "ctx", "TASK_STATE_WORKING", skill
            )

        async def poll_task(self, task_id, *, skill, context_id, reference_task_ids=()):
            del task_id, skill, context_id, reference_task_ids
            return A2AWorkerResult(
                "hermes",
                "research-detached",
                "ctx",
                "TASK_STATE_COMPLETED",
                "research.deep",
                {"report": "done", "sources": []},
            )

    class Delivery:
        async def send(self, **kwargs):
            del kwargs
            seen_contexts.append((current_tool_context(), CURRENT_TURN.get()))

    processing = ProcessingStore(tmp_path / "processing.db")
    store = A2AResearchStore(tmp_path / "research.db")
    tool = A2ADelegateTool(
        A2AWorkerRegistry(
            [A2AWorker(name="hermes", url="http://127.0.0.1:9900")],
            client_factory=lambda _: Client(),
        ),
        store=processing,
        delivery=Delivery(),
        pending_store=store,
    )
    tool.set_context("whatsapp", "chat@g.us")
    token = set_tool_context(ToolInvocationContext(channel="whatsapp", chat_id="chat@g.us"))
    turn_token = CURRENT_TURN.set(object())
    try:
        response = await tool.execute(
            worker="hermes", skill="research.deep", input={"question": "q", "idempotency_key": "r1"}
        )
    finally:
        CURRENT_TURN.reset(turn_token)
        reset_tool_context(token)
    assert "TASK_STATE_WORKING" in response
    while tool._background:
        await asyncio.sleep(0)
    assert seen_contexts == [(None, None)]
    assert store.pending() == ()


@pytest.mark.asyncio
async def test_research_poll_resumes_from_store_after_tool_restart(tmp_path: Path) -> None:
    path = tmp_path / "research.db"
    pending = A2AResearchStore(path)
    pending.put(
        PendingResearch(
            task_id="research-restart",
            worker="hermes",
            skill="research.deep",
            context_id="ctx-restart",
            reference_task_ids=("ref-1",),
            channel="whatsapp",
            chat_id="chat@g.us",
            effect_id="effect-restart",
        )
    )
    calls: list[tuple[str, str, str, tuple[str, ...]]] = []

    class Registry:
        async def poll_task(self, worker, task_id, *, skill, context_id, reference_task_ids=()):
            calls.append((worker, task_id, context_id, tuple(reference_task_ids)))
            return A2AWorkerResult(
                worker,
                task_id,
                context_id,
                "TASK_STATE_COMPLETED",
                skill,
                {"report": "done", "sources": []},
                tuple(reference_task_ids),
            )

    class Delivery:
        def __init__(self):
            self.sent = []

        async def send(self, **kwargs):
            self.sent.append(kwargs)

    delivery = Delivery()
    tool = A2ADelegateTool(Registry(), delivery=delivery, pending_store=A2AResearchStore(path))
    while tool._background:
        await asyncio.sleep(0)
    assert calls == [("hermes", "research-restart", "ctx-restart", ("ref-1",))]
    assert delivery.sent[0]["operation_ref"] == "a2a-result:effect-restart"
    assert A2AResearchStore(path).pending() == ()


def test_completed_report_is_durable_and_chat_scoped(tmp_path: Path) -> None:
    path = tmp_path / "research.db"
    A2AResearchStore(path).save_report(
        "a2a-effect-1", channel="whatsapp", chat_id="owners@g.us", content="Vollbericht mit Risiken"
    )

    reopened = A2AResearchStore(path)
    assert reopened.report("a2a-effect-1", channel="whatsapp", chat_id="owners@g.us") == "Vollbericht mit Risiken"
    assert reopened.report("a2a-effect-1", channel="whatsapp", chat_id="other@g.us") is None


def test_quoted_card_finds_only_its_chat_scoped_full_report(tmp_path: Path) -> None:
    from types import SimpleNamespace

    store = A2AResearchStore(tmp_path / "research.db")
    effect_id = "a2a-" + "a" * 32
    store.save_report(effect_id, channel="whatsapp", chat_id="first@g.us", content="Full report")

    class Processing:
        def effects_by_provider_message(self, channel, chat_id, message_id):
            return ("outbound-1",) if (channel, chat_id, message_id) == ("whatsapp", "first@g.us", "card-1") else ()

        def get_effect(self, effect_id):
            return SimpleNamespace(operation_key=f"send_text:whatsapp:first@g.us:a2a-result:a2a-{'a' * 32}:suffix")

    assert store.report_for_quote(Processing(), channel="whatsapp", chat_id="first@g.us", provider_message_id="card-1") == "Full report"
    assert store.report_for_quote(Processing(), channel="whatsapp", chat_id="other@g.us", provider_message_id="card-1") is None


def test_legacy_quoted_card_finds_full_report_by_card_text(tmp_path: Path) -> None:
    store = A2AResearchStore(tmp_path / "research.db")
    store.save_report(
        "a2a-" + "c" * 32, channel="whatsapp", chat_id="first@g.us",
        content="Full report", card="*Apple*: *HOLD* wegen teurer Bewertung.",
    )

    class NoManagedReceipts:
        def effects_by_provider_message(self, *args):
            return ()

    assert store.report_for_quote(
        NoManagedReceipts(), channel="whatsapp", chat_id="first@g.us", provider_message_id="legacy-1",
        quoted_text="Nachtrag zur Trading-Recherche\n\n*Apple*: *HOLD* wegen teurer Bewertung.",
    ) == "Full report"


def test_cross_chat_cached_card_omits_private_report_details(tmp_path: Path) -> None:
    store = A2AResearchStore(tmp_path / "research.db")
    store.save_report(
        "a2a-" + "d" * 32, channel="whatsapp", chat_id="private@s.whatsapp.net",
        content="My private portfolio details", canonical_user_id="owner-1", symbol="AAPL",
        card="*HOLD* based on my private portfolio details", signal="HOLD",
    )
    card = store.cached_card("owner-1", "AAPL")
    assert card is not None and "HOLD" in card
    assert "portfolio" not in card


@pytest.mark.asyncio
async def test_cached_trading_card_skips_new_a2a_and_quota_across_chats(tmp_path: Path) -> None:
    class Registry:
        names = ("hermes",)

        async def invoke_skill(self, *args, **kwargs):
            raise AssertionError("cache hit must not invoke Hermes")

    class Quota:
        def claim(self, *args, **kwargs):
            raise AssertionError("cache hit must not claim paid quota")

    store = A2AResearchStore(tmp_path / "research.db")
    store.save_report(
        "a2a-" + "b" * 32,
        channel="whatsapp", chat_id="first@g.us", content="Long report",
        canonical_user_id="owner-1", symbol="AAPL", card="*Hold*; valuation high",
        signal="HOLD",
    )
    tool = A2ADelegateTool(Registry(), pending_store=store, quota_governance=Quota())
    token = set_tool_context(ToolInvocationContext(
        channel="whatsapp", chat_id="second@g.us", canonical_user_id="owner-1",
        request_text="TradingGuru: Apple (AAPL) noch mal kurz?",
    ))
    try:
        result = await tool.execute(worker="hermes", skill="research.deep", input={"question": "Apple (AAPL)", "idempotency_key": "second"})
    finally:
        reset_tool_context(token)
    assert "CACHED" in result and "HOLD" in result
    assert store.cached_card("other-user", "AAPL") is None
    assert store.cached_card("owner-1", "MSFT") is None


@pytest.mark.asyncio
async def test_recent_report_without_clear_signal_does_not_start_another_job(tmp_path: Path) -> None:
    class Registry:
        names = ("hermes",)

        async def invoke_skill(self, *args, **kwargs):
            raise AssertionError("no second paid run for an already completed report")

    store = A2AResearchStore(tmp_path / "research.db")
    store.save_report(
        "a2a-" + "e" * 32, channel="whatsapp", chat_id="first@g.us", content="Full report",
        canonical_user_id="owner-1", symbol="AAPL", card="Apple analysis without verdict",
    )
    tool = A2ADelegateTool(Registry(), pending_store=store)
    token = set_tool_context(ToolInvocationContext(
        channel="whatsapp", chat_id="second@g.us", canonical_user_id="owner-1",
    ))
    try:
        result = await tool.execute(worker="hermes", skill="trading.analyze", input={
            "question": "Apple (AAPL)", "idempotency_key": "second",
        })
    finally:
        reset_tool_context(token)
    assert "not-sent" in result and "signal_unavailable" in result


@pytest.mark.asyncio
async def test_poll_persists_full_report_before_offering_it_in_card(tmp_path: Path) -> None:
    path = tmp_path / "research.db"
    store = A2AResearchStore(path)

    class Registry:
        async def poll_task(self, worker, task_id, *, skill, context_id, reference_task_ids=()):
            return A2AWorkerResult(
                worker, task_id, context_id, "TASK_STATE_COMPLETED", skill,
                {"report": "# Apple\n\n## Entscheidung\n\n**Hold**.\n\n## Risiken\n\nHohe Bewertung.", "sources": []},
            )

    class Delivery:
        async def send(self, **kwargs):
            assert store.report("a2a-effect-1", channel="whatsapp", chat_id="owners@g.us") is not None
            assert "*Hold*" in kwargs["content"]
            assert "Langfassung auf Abruf" in kwargs["content"]
            assert "Hohe Bewertung" not in kwargs["content"]
            return None

    tool = A2ADelegateTool(Registry(), delivery=Delivery(), pending_store=store)
    await tool._poll_research(
        "hermes", "task-1", "trading.analyze", "ctx-1", (), "a2a-effect-1", "whatsapp", "owners@g.us",
        canonical_user_id="owner-1", symbol="AAPL",
    )
    assert "Hohe Bewertung" in A2AResearchStore(path).report(
        "a2a-effect-1", channel="whatsapp", chat_id="owners@g.us"
    )
    assert "HOLD" in A2AResearchStore(path).cached_card("owner-1", "AAPL")


@pytest.mark.asyncio
async def test_research_pending_state_survives_an_unknown_delivery_outcome(
    tmp_path: Path,
) -> None:
    from types import SimpleNamespace

    path = tmp_path / "research.db"
    store = A2AResearchStore(path)
    pending = PendingResearch(
        task_id="research-unknown",
        worker="hermes",
        skill="research.deep",
        context_id="ctx-unknown",
        reference_task_ids=(),
        channel="whatsapp",
        chat_id="chat@g.us",
        effect_id="effect-unknown",
    )
    store.put(pending)

    class Registry:
        async def poll_task(self, worker, task_id, **kwargs):
            return A2AWorkerResult(
                worker,
                task_id,
                kwargs["context_id"],
                "TASK_STATE_COMPLETED",
                kwargs["skill"],
                {"report": "done", "sources": []},
            )

    class Delivery:
        async def send(self, **kwargs):
            return SimpleNamespace(state="unknown")

    tool = A2ADelegateTool(Registry(), delivery=Delivery(), pending_store=store)
    while tool._background:
        await asyncio.sleep(0)

    assert store.pending() == (pending,)


def test_workers_are_loopback_only_unless_explicitly_enabled() -> None:
    from yeoman_gateway.a2a.client import A2AWorkerConfigurationError

    with pytest.raises(A2AWorkerConfigurationError, match="loopback"):
        A2AWorker(name="remote", url="https://example.test/a2a")


def test_gateway_runtime_startup_resumes_durable_research() -> None:
    from types import SimpleNamespace

    from yeoman_gateway.app.bootstrap import GatewayRuntime

    calls: list[str] = []
    tool = SimpleNamespace(resume_pending_research=lambda: calls.append("resume"))
    runtime = object.__new__(GatewayRuntime)
    runtime.responder = SimpleNamespace(
        tools=SimpleNamespace(get=lambda name: tool if name == "a2a_delegate" else None)
    )

    runtime._resume_a2a_research()

    assert calls == ["resume"]


def test_router_exposes_gateway_store() -> None:
    from types import SimpleNamespace

    from yeoman_gateway.processing.dispatch import IntentEffectRouter
    from yeoman_shared.config.schema import Config

    store = object()
    assert (
        IntentEffectRouter(
            gateway=SimpleNamespace(store=store),
            config=Config.model_validate({"processing": {"enabled": True}}),
        ).store
        is store
    )


def test_processing_fence_keeps_a2a_delegate_reachable() -> None:
    from yeoman_gateway.agent.tools.registry import ToolRegistry
    from yeoman_gateway.processing.dispatch import (
        NON_MIGRATED_CAPABILITIES,
        disable_non_migrated_tools,
    )

    class Tool:
        def __init__(self, name: str) -> None:
            self.name = name

        def to_schema(self):
            return {"type": "function", "function": {"name": self.name}}

        def validate_params(self, params):
            return []

        async def execute(self, **kwargs):
            return ""

    registry = ToolRegistry()
    for name in ("message", "a2a_delegate", *NON_MIGRATED_CAPABILITIES):
        registry.register(Tool(name))
    assert "a2a_delegate" not in disable_non_migrated_tools(registry)


def _delegate_tool(processing: ProcessingStore, client: object) -> A2ADelegateTool:
    return A2ADelegateTool(
        A2AWorkerRegistry(
            [A2AWorker(name="hermes", url="http://127.0.0.1:9900")],
            client_factory=lambda _: client,
        ),
        store=processing,
    )


@pytest.mark.asyncio
async def test_explicit_trading_guru_request_uses_trading_analyze(tmp_path: Path) -> None:
    processing = ProcessingStore(tmp_path / "processing.db")
    calls: list[str] = []

    class Client:
        async def invoke_skill(self, skill, input, *, context_id=None, reference_task_ids=()):
            calls.append(skill)
            return A2AWorkerResult("hermes", "task-1", context_id or "ctx", "TASK_STATE_COMPLETED", skill, {"report": "ok"})

    tool = _delegate_tool(processing, Client())
    token = set_tool_context(ToolInvocationContext(channel="whatsapp", chat_id="chat@g.us", is_owner=True, request_text="Bitte Apple an TradingGuru delegieren"))
    try:
        await tool.execute(worker="hermes", skill="research.deep", input={"question": "Apple analysieren", "idempotency_key": "apple-1"})
    finally:
        reset_tool_context(token)

    assert calls == ["trading.analyze"]


@pytest.mark.asyncio
async def test_ticker_alone_does_not_override_explicit_deep_research(tmp_path: Path) -> None:
    processing = ProcessingStore(tmp_path / "processing.db")
    calls: list[str] = []

    class Client:
        async def invoke_skill(self, skill, input, *, context_id=None, reference_task_ids=()):
            calls.append(skill)
            return A2AWorkerResult("hermes", "task-1", context_id or "ctx", "TASK_STATE_COMPLETED", skill, {"report": "ok"})

    tool = _delegate_tool(processing, Client())
    token = set_tool_context(ToolInvocationContext(
        channel="whatsapp", chat_id="chat@g.us", is_owner=True,
        request_text="Deep Research zu Microsoft (MSFT) und Lieferketten",
    ))
    try:
        await tool.execute(worker="hermes", skill="research.deep", input={
            "question": "Microsoft (MSFT) und Lieferketten", "idempotency_key": "msft-deep-1",
        })
    finally:
        reset_tool_context(token)
    assert calls == ["research.deep"]


@pytest.mark.asyncio
async def test_local_rejection_can_be_retried_with_corrected_arguments(tmp_path: Path) -> None:
    """A locally rejected invocation never left the host, so a corrected retry must go out.

    Production 2026-09-14: attempt 1 was rejected by local schema validation, attempt 2
    corrected the arguments and was answered with "conflict", attempt 3 with "duplicate" -
    the delegation never reached Hermes.
    """
    from yeoman_gateway.a2a.client import A2AProtocolError
    from yeoman_gateway.a2a.contracts import A2AContractValidationError

    calls: list[dict[str, object]] = []

    class Client:
        async def invoke_skill(self, skill, input, *, context_id=None, reference_task_ids=()):
            calls.append(dict(input))
            if len(calls) == 1:
                raise A2AProtocolError(
                    "A2A invocation rejected locally: $: required (missing required fields: idempotency_key)",
                    retryable=True,
                ) from A2AContractValidationError(
                    "$", "required", detail="missing required fields: idempotency_key"
                )
            return A2AWorkerResult(
                "hermes", "task-9", "ctx-9", "TASK_STATE_COMPLETED", skill, {"report": "ok"}
            )

    processing = ProcessingStore(tmp_path / "processing.db")
    tool = _delegate_tool(processing, Client())

    first = await tool.execute(
        worker="hermes", skill="research.deep", input={"question": "Analyse ORCL"}
    )
    second = await tool.execute(
        worker="hermes",
        skill="research.deep",
        input={"question": "Analyse ORCL", "idempotency_key": "orcl-1"},
    )

    assert "rejected locally" in first
    assert "not-sent | rejected" in first, "the note must say the invocation never left the host"
    assert "TASK_STATE_COMPLETED" in second, second
    assert calls == [{"question": "Analyse ORCL"}, {"question": "Analyse ORCL", "idempotency_key": "orcl-1"}]
    states = [processing.effect_state(effect.effect_id) for effect in processing.list_effects()]
    assert "failed" in states, states


@pytest.mark.asyncio
async def test_identical_retry_after_a_local_rejection_is_still_not_resent(tmp_path: Path) -> None:
    """The idempotency contract stays intact: same payload in the same window is one effect."""
    from yeoman_gateway.a2a.client import A2AProtocolError
    from yeoman_gateway.a2a.contracts import A2AContractValidationError

    calls: list[dict[str, object]] = []

    class Client:
        async def invoke_skill(self, skill, input, *, context_id=None, reference_task_ids=()):
            calls.append(dict(input))
            raise A2AProtocolError("A2A invocation rejected locally: $: required") from (
                A2AContractValidationError("$", "required", detail="missing required fields: x")
            )

    processing = ProcessingStore(tmp_path / "processing.db")
    tool = _delegate_tool(processing, Client())
    payload = {"question": "Analyse ORCL"}

    first = await tool.execute(worker="hermes", skill="research.deep", input=payload)
    second = await tool.execute(worker="hermes", skill="research.deep", input=payload)

    assert "rejected locally" in first
    assert "duplicate" in second
    assert len(calls) == 1, "the store must still collapse an identical retry"


@pytest.mark.asyncio
async def test_transport_failure_still_blocks_a_retry_as_unproven(tmp_path: Path) -> None:
    """An unproven remote outcome keeps the store's reconciliation contract.

    A local rejection is proven not-executed and may be retried; a transport failure is not,
    so the identical retry is collapsed and the client must not be called a second time.
    """
    from yeoman_gateway.a2a.client import A2ATransportError

    calls: list[dict[str, object]] = []

    class Client:
        async def invoke_skill(self, skill, input, *, context_id=None, reference_task_ids=()):
            calls.append(dict(input))
            raise A2ATransportError("hermes unreachable")

    processing = ProcessingStore(tmp_path / "processing.db")
    tool = _delegate_tool(processing, Client())
    payload = {"question": "Analyse ORCL", "idempotency_key": "orcl-2"}

    with pytest.raises(A2ATransportError):
        await tool.execute(worker="hermes", skill="research.deep", input=payload)

    states = [processing.effect_state(effect.effect_id) for effect in processing.list_effects()]
    assert states == ["unknown"], states

    second = await tool.execute(worker="hermes", skill="research.deep", input=payload)
    assert "duplicate" in second, second
    assert len(calls) == 1, "an unproven effect must not be re-sent without reconciliation"


@pytest.mark.asyncio
async def test_a_claim_conflict_is_reported_and_logged(tmp_path: Path) -> None:
    """Defensive path: the store reports a conflict before anything is sent."""
    from yeoman_gateway.processing.models import EffectConflictError

    calls: list[dict[str, object]] = []

    class Client:
        async def invoke_skill(self, skill, input, *, context_id=None, reference_task_ids=()):
            calls.append(dict(input))
            raise AssertionError("a conflicting claim must never reach the worker")

    class ConflictingStore(ProcessingStore):
        def enqueue_effect(self, **kwargs):
            raise EffectConflictError("same operation key, different payload")

    tool = _delegate_tool(ConflictingStore(tmp_path / "processing.db"), Client())

    response = await tool.execute(
        worker="hermes", skill="research.deep", input={"question": "Analyse ORCL"}
    )

    assert response == "[hermes | not-sent | conflict]"
    assert calls == []


def test_research_output_is_rendered_for_chat_not_dumped_as_json() -> None:
    """The payload Hermes returns is JSON with a reasoning preamble; the chat must not see either."""
    from yeoman_gateway.agent.tools.a2a import render_research_output

    payload = {
        "report": (
            "💭 **Reasoning:**\n```\n**Planning concise German summary**\n"
            "**Checking sources**\n```\n\n"
            "# ORCL — Analyse\n\n## Entscheidung\n\n**Underweight** bei 150 USD.\n\n"
            "- RPO 664 Mrd. USD\n- FCF negativ\n\n"
            "Vollständiger Report: /home/deploy/.hermes/tradingagents/reports/ORCL/2026-09-14.md"
        ),
        "sources": [
            {"title": "Oracle IR", "url": "https://investor.oracle.com/q1fy27"},
            {"title": "Yahoo Finance", "url": "https://finance.yahoo.com/quote/ORCL"},
        ],
    }

    rendered = render_research_output(payload)

    assert "Reasoning" not in rendered, rendered
    assert "```" not in rendered, rendered
    assert '{"report"' not in rendered
    assert "# ORCL" not in rendered and "## " not in rendered
    assert "*Underweight*" in rendered
    assert "• RPO 664 Mrd. USD" in rendered
    assert "Quellen:" in rendered
    assert "https://investor.oracle.com/q1fy27" in rendered
    assert "/home/deploy/.hermes" not in rendered, "keine Serverpfade im Chat"


def test_research_output_without_sources_or_report_stays_readable() -> None:
    from yeoman_gateway.agent.tools.a2a import render_research_output

    assert render_research_output({"report": "Nur Text."}) == "Nur Text."
    plain = render_research_output("error=POLL_TIMEOUT retryable=True")
    assert plain == "error=POLL_TIMEOUT retryable=True"
    assert render_research_output({}) == ""
    assert render_research_output(None) == ""


def test_follow_up_content_uses_the_rendered_report() -> None:
    """Header plus rendered body: no JSON, no reasoning, no server path."""
    from yeoman_gateway.agent.tools.a2a import render_research_output

    payload = {
        "report": "💭 **Reasoning:**\n```\nplan\n```\n\n# MSFT\n\n**Overweight**.\n\n"
        "Report: /home/deploy/.hermes/tradingagents/reports/MSFT/2026-09-14.md",
        "sources": [],
    }
    body = render_research_output(payload)
    assert body.startswith("MSFT"), body
    assert "Overweight" in body
    assert "Reasoning" not in body


def test_research_output_has_no_markdown_line_break_artifacts() -> None:
    from yeoman_gateway.agent.tools.a2a import render_research_output

    rendered = render_research_output(
        {"report": "Zeile eins  \nZeile zwei\n\nAbsatz  \n"}, mode="full"
    )
    assert "  \n" not in rendered, repr(rendered)
    assert rendered == "Zeile eins\nZeile zwei\n\nAbsatz"


_LONG_REPORT = """💭 **Reasoning:**
```
Plan A
Plan B
```

# ORCL — TradingAgents-Analyse für heute, 14.09.2026

## Entscheidung

**Underweight** — bestehende Position reduzieren, keine neuen Longs vor dem FOMC.

## Begründung

- Oracle meldete 30 % Umsatzwachstum, aber negativen Free Cashflow von 5 Mrd. USD.
- Ablehnung am fallenden 200-Tage-SMA bei 166,82 USD.
- RPO von 664 Mrd. USD stützt die Story, die Umwandlung in Cash bleibt offen.
- Bewertung bleibt hoch, Refinanzierung teuer.

## Risiken

- FOMC nächste Woche.

Report: /home/deploy/.hermes/tradingagents/reports/ORCL/2026-09-14.md
"""


def test_card_mode_is_a_short_summary_of_a_long_report() -> None:
    from yeoman_gateway.agent.tools.a2a import render_research_output

    card = render_research_output(
        {"report": _LONG_REPORT, "sources": [{"title": "Oracle IR", "url": "https://oracle.com/ir"}]},
        mode="card",
    )

    assert card.startswith("ORCL — TradingAgents-Analyse für heute, 14.09.2026"), card
    assert "*Underweight*" in card
    assert "• Oracle meldete 30 % Umsatzwachstum" in card
    assert card.count("• ") <= 5, card  # four reasons plus the source list
    assert "FOMC nächste Woche" not in card, "Risiko-Abschnitt gehört nur in die Langfassung"
    assert "Reasoning" not in card and "```" not in card and '{"report"' not in card
    assert "/home/deploy" not in card
    assert "https://oracle.com/ir" in card
    assert len(card) <= 1400, len(card)
    assert "Langfassung" in card


def test_card_prioritizes_trading_decision_over_earlier_market_summary() -> None:
    from yeoman_gateway.agent.tools.a2a import render_research_output

    report = (
        "# Apple\n\n## Kurzfazit\n\nKurs 331 USD.\n- 52-Wochen-Spanne 236 bis 344 USD.\n\n"
        "## Entscheidung\n\n**Hold** – kein neuer Kauf.\n\n## Begründung\n\n"
        "- Bewertung ist hoch.\n- Cashflow bleibt stark.\n"
    )

    card = render_research_output({"report": report})

    assert "*Hold*" in card
    assert "Bewertung ist hoch" in card
    assert "52-Wochen-Spanne" not in card


def test_card_includes_signal_even_when_report_has_no_decision_heading() -> None:
    from yeoman_gateway.agent.tools.a2a import render_research_output

    report = "# Apple\n\n## Kurzfazit\n\nKurs 331 USD.\n\n## Trade Signal\n\n**SELL** wegen Bewertung."
    card = render_research_output({"report": report})
    assert "*SELL*" in card


def test_cached_signal_comes_from_decision_not_title() -> None:
    from yeoman_gateway.agent.tools.a2a import _trading_signal

    report = "# Should I BUY, SELL or HOLD AAPL?\n\n## Entscheidung\n\n**HOLD** wegen Bewertung."
    assert _trading_signal(report) == "HOLD"
    assert _trading_signal("# Should I BUY, SELL or HOLD AAPL?\n\nNo decision yet.") == ""


def test_card_mode_falls_back_to_a_short_excerpt_without_a_decision_heading() -> None:
    from yeoman_gateway.agent.tools.a2a import render_research_output

    card = render_research_output({"report": "## Lage\n\nKurzer Text ohne Entscheidung."}, mode="card")
    assert "Lage" in card
    assert "Kurzer Text ohne Entscheidung." in card


def test_full_mode_keeps_the_whole_report() -> None:
    from yeoman_gateway.agent.tools.a2a import render_research_output

    full = render_research_output({"report": _LONG_REPORT}, mode="full")
    # Full mode keeps every section; headings are flattened for chat, not dropped.
    assert "Risiken" in full
    assert "FOMC nächste Woche" in full
    assert "Bewertung bleibt hoch" in full
    assert "Langfassung auf Abruf." not in full


def test_card_mode_keeps_errors_and_short_answers_unchanged() -> None:
    from yeoman_gateway.agent.tools.a2a import render_research_output

    assert render_research_output("error=POLL_TIMEOUT retryable=True", mode="card") == (
        "error=POLL_TIMEOUT retryable=True"
    )
    short = "Der Markt ist zu; ORCL schloss bei 150,28 USD."
    assert render_research_output({"report": short}, mode="card").startswith(short)
