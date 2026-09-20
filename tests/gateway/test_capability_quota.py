from __future__ import annotations

import asyncio
import inspect
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import ValidationError
from yeoman_gateway.a2a.client import A2AProtocolError, A2AWorker, A2AWorkerResult
from yeoman_gateway.a2a.registry import A2AWorkerRegistry
from yeoman_gateway.adapters.policy_engine import EnginePolicyAdapter
from yeoman_gateway.adapters.responder_llm import LLMResponder
from yeoman_gateway.agent.subagent import SubagentManager
from yeoman_gateway.agent.tools.a2a import A2ADelegateTool
from yeoman_gateway.agent.tools.spawn import SpawnTool
from yeoman_gateway.agent.tools.web import DeepResearchTool
from yeoman_gateway.policy.engine import PolicyEngine
from yeoman_gateway.policy.identity import canonical_user_id
from yeoman_gateway.policy.loader import save_policy
from yeoman_gateway.policy.schema import PolicyConfig
from yeoman_gateway.processing.models import PolicySnapshot
from yeoman_gateway.processing.quota import CapabilityQuotaGovernance, quota_key_for
from yeoman_gateway.processing.store import SCHEMA_VERSION, ProcessingStore
from yeoman_gateway.processing.tool_context import (
    ToolInvocationContext,
    reset_tool_context,
    set_tool_context,
)


def _context(user: str = "whatsapp:491700000001", chat: str = "a@g.us") -> ToolInvocationContext:
    return ToolInvocationContext(
        channel="whatsapp",
        chat_id=chat,
        canonical_user_id=user,
    )


def _policy(*, deep: int = 86_400, trading: int = 86_400) -> PolicyConfig:
    return PolicyConfig.model_validate(
        {
            "capabilityQuotas": {
                "deep_research": {"cooldownSeconds": deep},
                "trading_guru": {"cooldownSeconds": trading},
            }
        }
    )


class _PolicyProvider:
    def __init__(self, policy: PolicyConfig, *, healthy: bool = True) -> None:
        self.snapshot = PolicySnapshot(
            version="test",
            policy_hash="test",
            policy=policy,
            loaded_ms=1,
            healthy=healthy,
            source="test",
            error=None if healthy else "reload failed",
        )

    def policy_snapshot(self) -> PolicySnapshot:
        return self.snapshot


def _governance(
    store: ProcessingStore,
    *,
    policy: PolicyConfig | None = None,
    healthy: bool = True,
    now: int = 1_000,
) -> CapabilityQuotaGovernance:
    return CapabilityQuotaGovernance(
        store=store,
        policy_provider=_PolicyProvider(policy or _policy(), healthy=healthy),
        clock=lambda: now,
    )


def test_quota_key_mapping_is_explicit() -> None:
    assert quota_key_for("deep_research", {}) == "deep_research"
    assert quota_key_for("a2a_delegate", {"skill": "research.deep"}) == "deep_research"
    assert quota_key_for("a2a_delegate", {"skill": "trading.analyze"}) == "trading_guru"
    assert quota_key_for("a2a_delegate", {"skill": "search.web"}) is None
    assert quota_key_for("some_other_tool", {}) is None


def test_canonical_whatsapp_identity_uses_only_trusted_phone_metadata() -> None:
    metadata = {
        "sender_phone_jid": "491700000001:7@s.whatsapp.net",
        "participant_lid": "123456789@lid",
    }
    assert canonical_user_id("whatsapp", "123456789@lid", metadata) == "whatsapp:491700000001"
    assert canonical_user_id("whatsapp", "ignored", {"sender_name": "491700000001"}) == ""
    assert canonical_user_id("whatsapp", "123456789@lid", {**metadata, "lid_conflict": True}) == ""
    assert canonical_user_id("telegram", "491700000001", metadata) == ""


def test_store_claims_across_chats_and_at_exact_rolling_boundary(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "processing.db")
    assert store.schema_version == SCHEMA_VERSION
    assert store.claim_capability(
        "whatsapp:15550001", "deep_research", "a", cooldown_ms=86_400_000, now_ms=1_000
    ) == (True, 0)
    assert store.claim_capability(
        "whatsapp:15550001", "deep_research", "b", cooldown_ms=86_400_000, now_ms=86_400_999
    ) == (False, 86_401_000)
    assert store.claim_capability(
        "whatsapp:15550001", "deep_research", "c", cooldown_ms=86_400_000, now_ms=86_401_000
    ) == (True, 0)
    assert not store.release_capability("whatsapp:15550001", "deep_research", "a")


def test_store_parallel_connections_allow_one_claim(tmp_path: Path) -> None:
    path = tmp_path / "processing.db"
    stores = [ProcessingStore(path), ProcessingStore(path)]

    def claim(index: int) -> tuple[bool, int]:
        return stores[index].claim_capability(
            "whatsapp:15550001",
            "trading_guru",
            str(index),
            cooldown_ms=86_400_000,
            now_ms=1_000,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, range(2)))
    assert sum(result[0] for result in results) == 1


def test_store_rejects_empty_claim_fields(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "processing.db")
    with pytest.raises(ValueError):
        store.claim_capability("", "deep_research", "a", cooldown_ms=1, now_ms=1)
    with pytest.raises(ValueError):
        store.claim_capability("u", "deep_research", "a", cooldown_ms=0, now_ms=1)
    with pytest.raises(ValueError):
        store.release_capability("u", "deep_research", "")


def test_governance_keeps_capabilities_separate_and_owner_is_verified_context() -> None:
    store = ProcessingStore(":memory:")
    governance = _governance(store)

    token = set_tool_context(_context())
    try:
        first = governance.claim("deep_research", {})
        second = governance.claim("a2a_delegate", {"skill": "trading.analyze"})
    finally:
        reset_tool_context(token)
    assert first.allowed and first.claim_id
    assert second.allowed and second.claim_id

    owner = CapabilityQuotaGovernance(
        store=store,
        policy_provider=_PolicyProvider(_policy()),
        clock=lambda: 1_000,
    )
    token = set_tool_context(
        ToolInvocationContext(channel="whatsapp", chat_id="a@g.us", is_owner=True)
    )
    try:
        decision = owner.claim("deep_research", {})
    finally:
        reset_tool_context(token)
    assert decision.allowed
    assert not decision.claim_id


def test_governance_key_is_global_across_chats() -> None:
    store = ProcessingStore(":memory:")
    governance = _governance(store)
    first_context = _context(chat="a@g.us")
    second_context = _context(chat="b@g.us")
    other_user = _context("whatsapp:491700000002", "b@g.us")
    assert governance.claim("deep_research", {}, context=first_context).allowed
    assert governance.claim("deep_research", {}, context=second_context).reason == "quota_exhausted"
    assert governance.claim("deep_research", {}, context=other_user).allowed


def test_governance_blocks_unresolved_identity_and_unhealthy_policy() -> None:
    store = ProcessingStore(":memory:")
    token = set_tool_context(ToolInvocationContext(channel="whatsapp", chat_id="a@g.us"))
    try:
        unresolved = _governance(store).claim("deep_research", {})
        unavailable = _governance(store, healthy=False).claim(
            "deep_research", {}, context=_context()
        )
    finally:
        reset_tool_context(token)
    assert unresolved.reason == "identity_unresolved"
    assert unavailable.reason == "quota_unavailable"


def test_policy_rejects_nonpositive_quota_cooldown() -> None:
    with pytest.raises(ValidationError):
        _policy(deep=0)


def test_governance_uses_current_policy_reload_state(tmp_path: Path) -> None:
    path = tmp_path / "policy.json"
    policy = _policy(deep=1)
    save_policy(policy, path)
    adapter = EnginePolicyAdapter(
        engine=PolicyEngine(policy, workspace=tmp_path, apply_channels={"whatsapp"}),
        known_tools={"list_dir", "read_file", "web_search", "web_fetch"},
        policy_path=path,
        reload_on_change=True,
        reload_check_interval_seconds=0.1,
        workspace=tmp_path,
    )
    governance = CapabilityQuotaGovernance(
        store=ProcessingStore(tmp_path / "processing.db"),
        policy_provider=adapter,
        clock=lambda: 1_000,
    )
    assert governance.claim("deep_research", {}, context=_context()).allowed

    changed = policy.model_dump(by_alias=True, exclude_none=True)
    changed["capabilityQuotas"]["deep_research"]["cooldownSeconds"] = 2
    previous = path.stat().st_mtime_ns
    path.write_text(json.dumps(changed), encoding="utf-8")
    os.utime(path, ns=(previous, max(time.time_ns(), previous + 1)))
    adapter._last_reload_check = 0.0
    current = adapter.current_policy_snapshot()
    assert current.policy.capability_quotas["deep_research"].cooldown_seconds == 2

    invalid = dict(changed)
    invalid["unknownField"] = True
    previous = path.stat().st_mtime_ns
    path.write_text(json.dumps(invalid), encoding="utf-8")
    os.utime(path, ns=(previous, max(time.time_ns(), previous + 1)))
    adapter._last_reload_check = 0.0
    assert governance.claim("deep_research", {}, context=_context("whatsapp:491700000002")).reason == "quota_unavailable"

    path.write_text(json.dumps(changed), encoding="utf-8")
    adapter._last_reload_check = 0.0
    assert governance.claim("deep_research", {}, context=_context("whatsapp:491700000003")).allowed


def test_process_direct_does_not_default_to_owner() -> None:
    assert inspect.signature(LLMResponder.process_direct).parameters["is_owner"].default is False


@pytest.mark.asyncio
async def test_deep_research_claims_once_before_tavily_and_keeps_claim_on_error(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "processing.db")
    governance = _governance(store)
    tool = DeepResearchTool(api_key="test", quota_governance=governance)
    calls = 0

    async def search(query: str, search_depth: str = "basic", max_results: int = 5):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("provider failed")
        return {"results": []}

    tool._search = search  # type: ignore[method-assign]
    token = set_tool_context(_context())
    try:
        first = await tool.execute("q", depth="basic")
        second = await tool.execute("q", depth="basic")
    finally:
        reset_tool_context(token)
    assert first == "Error: provider failed"
    assert "quota_exhausted" in second
    assert calls == 1


@pytest.mark.asyncio
async def test_deep_research_rejects_invalid_input_before_claim(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "processing.db")
    tool = DeepResearchTool(api_key="test", quota_governance=_governance(store))
    calls = 0

    async def search(query: str, search_depth: str = "basic", max_results: int = 5):
        nonlocal calls
        calls += 1
        return {"results": []}

    tool._search = search  # type: ignore[method-assign]
    token = set_tool_context(_context())
    try:
        assert await tool.execute("") == "Error: query is required"
        assert "Sources" in await tool.execute("q", depth="basic")
    finally:
        reset_tool_context(token)
    assert calls == 1


@pytest.mark.asyncio
async def test_a2a_trading_is_hermes_only_and_counts_submitted_order(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []

    class Client:
        async def invoke_skill(self, skill, input, *, context_id=None, reference_task_ids=()):
            del input, reference_task_ids
            calls.append(("invoke", skill))
            return A2AWorkerResult("hermes", "task", context_id or "ctx", "TASK_STATE_COMPLETED", skill, {"ok": True})

    registry = A2AWorkerRegistry(
        [
            A2AWorker(name="hermes", url="http://127.0.0.1:9900"),
            A2AWorker(name="rogue", url="http://127.0.0.1:9901"),
        ],
        client_factory=lambda _: Client(),
    )
    store = ProcessingStore(tmp_path / "processing.db")
    tool = A2ADelegateTool(registry, store=store, quota_governance=_governance(store))
    token = set_tool_context(_context())
    try:
        rejected = await tool.execute(
            worker="rogue", skill="trading.analyze", input={"question": "q"}
        )
        accepted = await tool.execute(
            worker="hermes", skill="trading.analyze", input={"question": "q"}
        )
        exhausted = await tool.execute(
            worker="hermes", skill="trading.analyze", input={"question": "q2"}
        )
    finally:
        reset_tool_context(token)
    assert "worker-binding" in rejected
    assert "TASK_STATE_COMPLETED" in accepted
    assert "quota_exhausted" in exhausted
    assert calls == [("invoke", "trading.analyze")]


@pytest.mark.asyncio
async def test_a2a_local_contract_rejection_releases_only_its_quota_claim(tmp_path: Path) -> None:
    from yeoman_gateway.a2a.contracts import A2AContractValidationError

    calls = 0

    class Client:
        async def invoke_skill(self, skill, input, *, context_id=None, reference_task_ids=()):
            nonlocal calls
            del skill, input, context_id, reference_task_ids
            calls += 1
            try:
                raise A2AContractValidationError("$.input", "invalid")
            except A2AContractValidationError as cause:
                raise A2AProtocolError("local rejection") from cause

    registry = A2AWorkerRegistry(
        [A2AWorker(name="hermes", url="http://127.0.0.1:9900")],
        client_factory=lambda _: Client(),
    )
    store = ProcessingStore(tmp_path / "processing.db")
    tool = A2ADelegateTool(registry, store=store, quota_governance=_governance(store))
    token = set_tool_context(_context())
    try:
        first = await tool.execute(
            worker="hermes", skill="trading.analyze", input={"question": "q1"}
        )
        second = await tool.execute(
            worker="hermes", skill="trading.analyze", input={"question": "q2"}
        )
    finally:
        reset_tool_context(token)
    assert "not-sent | rejected" in first
    assert "not-sent | rejected" in second
    assert calls == 2


@pytest.mark.asyncio
async def test_spawn_explicitly_preserves_original_tool_context() -> None:
    seen: list[ToolInvocationContext | None] = []
    manager = object.__new__(SubagentManager)
    manager._running_tasks = {}

    async def run_subagent(task_id, task, label, origin):
        del task_id, task, label, origin
        from yeoman_gateway.processing.tool_context import current_tool_context

        seen.append(current_tool_context())

    manager._run_subagent = run_subagent
    spawn = SpawnTool(manager)
    context = _context("whatsapp:491700000009", "b@g.us")
    token = set_tool_context(context)
    try:
        await spawn.execute(task="research")
        while manager._running_tasks:
            await asyncio.sleep(0)
    finally:
        reset_tool_context(token)
    assert seen == [context]
