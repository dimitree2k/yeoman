import asyncio
import json
import os
import platform
import shutil
import time
from pathlib import Path
from typing import Any

import pytest

from nanobot.adapters.responder_llm import LLMResponder
from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.exec_isolation import (
    CommandResult,
    ExecSandboxManager,
    MountAllowlist,
    SandboxPreemptedError,
    SandboxTimeoutError,
)
from nanobot.agent.tools.pi_stats import PiStatsTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.shell import ExecTool
from nanobot.bus.queue import MessageBus
from nanobot.config.loader import _migrate_config, convert_keys, convert_to_camel
from nanobot.config.schema import Config, ExecToolConfig
from nanobot.providers.base import LLMProvider, LLMResponse
from nanobot.utils.helpers import get_workspace_path


class SampleTool(Tool):
    @property
    def name(self) -> str:
        return "sample"

    @property
    def description(self) -> str:
        return "sample tool"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 2},
                "count": {"type": "integer", "minimum": 1, "maximum": 10},
                "mode": {"type": "string", "enum": ["fast", "full"]},
                "meta": {
                    "type": "object",
                    "properties": {
                        "tag": {"type": "string"},
                        "flags": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["tag"],
                },
            },
            "required": ["query", "count"],
        }

    async def execute(self, **kwargs: Any) -> str:
        return "ok"


def test_validate_params_missing_required() -> None:
    tool = SampleTool()
    errors = tool.validate_params({"query": "hi"})
    assert "missing required count" in "; ".join(errors)


def test_validate_params_type_and_range() -> None:
    tool = SampleTool()
    errors = tool.validate_params({"query": "hi", "count": 0})
    assert any("count must be >= 1" in e for e in errors)

    errors = tool.validate_params({"query": "hi", "count": "2"})
    assert any("count should be integer" in e for e in errors)


def test_validate_params_enum_and_min_length() -> None:
    tool = SampleTool()
    errors = tool.validate_params({"query": "h", "count": 2, "mode": "slow"})
    assert any("query must be at least 2 chars" in e for e in errors)
    assert any("mode must be one of" in e for e in errors)


def test_validate_params_nested_object_and_array() -> None:
    tool = SampleTool()
    errors = tool.validate_params(
        {
            "query": "hi",
            "count": 2,
            "meta": {"flags": [1, "ok"]},
        }
    )
    assert any("missing required meta.tag" in e for e in errors)
    assert any("meta.flags[0] should be string" in e for e in errors)


def test_validate_params_ignores_unknown_fields() -> None:
    tool = SampleTool()
    errors = tool.validate_params({"query": "hi", "count": 2, "extra": "x"})
    assert errors == []


async def test_registry_returns_validation_error() -> None:
    reg = ToolRegistry()
    reg.register(SampleTool())
    result = await reg.execute("sample", {"query": "hi"})
    assert "Invalid parameters" in result


async def test_exec_tool_blocks_dangerous_command(tmp_path: Path) -> None:
    tool = ExecTool(timeout=1, working_dir=str(tmp_path))
    result = await tool.execute("rm -rf /")
    assert "blocked by safety guard" in result


async def test_exec_tool_timeout_and_recovery(tmp_path: Path) -> None:
    tool = ExecTool(timeout=1, working_dir=str(tmp_path))
    timed_out = await tool.execute("sleep 2")
    assert "timed out" in timed_out

    recovered = await tool.execute("echo ok")
    assert "ok" in recovered


async def test_pi_stats_tool_json_format() -> None:
    tool = PiStatsTool()
    result = await tool.execute(format="json")
    data = json.loads(result)
    assert "temperature_c" in data
    assert "cpu_usage_pct" in data
    assert "memory_total_mb" in data
    assert "disk_root_used_gb" in data


async def test_pi_stats_tool_text_format() -> None:
    tool = PiStatsTool()
    result = await tool.execute(format="text")
    assert "Raspberry Pi Stats" in result
    assert "temperature_c:" in result
    assert "cpu_usage_pct:" in result


def test_exec_isolation_defaults_and_camel_case_roundtrip() -> None:
    cfg = Config()
    iso = cfg.tools.exec.isolation
    assert iso.enabled is False
    assert iso.backend == "bubblewrap"
    assert iso.batch_session_idle_seconds == 600
    assert iso.max_containers == 5
    assert iso.pressure_policy == "preempt_oldest_active"
    assert cfg.agents.defaults.timing_logs_enabled is False
    assert cfg.memory.enabled is True
    assert cfg.memory.backend == "sqlite_fts"
    assert cfg.memory.capture.enabled is True
    assert cfg.memory.recall.max_results == 8
    assert cfg.memory.embedding.enabled is False

    data = {
        "agents": {
            "defaults": {
                "timingLogsEnabled": True,
            }
        },
        "tools": {
            "exec": {
                "isolation": {
                    "enabled": True,
                    "batchSessionIdleSeconds": 123,
                    "maxContainers": 7,
                }
            }
        },
        "memory": {
            "backend": "reserved_hybrid",
            "capture": {
                "enabled": False,
                "minConfidence": 0.9,
            }
        },
    }
    loaded = Config.model_validate(convert_keys(data))
    assert loaded.tools.exec.isolation.enabled is True
    assert loaded.tools.exec.isolation.batch_session_idle_seconds == 123
    assert loaded.tools.exec.isolation.max_containers == 7
    assert loaded.agents.defaults.timing_logs_enabled is True
    assert loaded.memory.backend == "reserved_hybrid"
    assert loaded.memory.capture.enabled is False
    assert loaded.memory.capture.min_confidence == 0.9

    dumped = convert_to_camel(loaded.model_dump())
    assert dumped["tools"]["exec"]["isolation"]["batchSessionIdleSeconds"] == 123
    assert dumped["tools"]["exec"]["isolation"]["maxContainers"] == 7
    assert dumped["agents"]["defaults"]["timingLogsEnabled"] is True
    assert dumped["memory"]["backend"] == "reserved_hybrid"
    assert dumped["memory"]["capture"]["enabled"] is False
    assert dumped["memory"]["capture"]["minConfidence"] == 0.9


def test_config_migration_for_legacy_isolation_keys() -> None:
    raw = {
        "tools": {
            "exec": {
                "isolationEnabled": True,
                "isolationBackend": "bubblewrap",
                "isolation": {"allowlist": "/tmp/allow.json"},
            }
        }
    }
    migrated = _migrate_config(raw)
    isolation = migrated["tools"]["exec"]["isolation"]
    assert isolation["enabled"] is True
    assert isolation["backend"] == "bubblewrap"
    assert isolation["allowlistPath"] == "/tmp/allow.json"


def test_workspace_path_relative_is_scoped_under_nanobot_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = Config.model_validate(convert_keys({"agents": {"defaults": {"workspace": "workspace"}}}))
    assert cfg.workspace_path == tmp_path / ".nanobot" / "workspace"


def test_get_workspace_path_relative_is_scoped_under_nanobot_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    relative = get_workspace_path("workspace")
    absolute_target = tmp_path / "custom-workspace"
    absolute = get_workspace_path(str(absolute_target))

    assert relative == tmp_path / ".nanobot" / "workspace"
    assert relative.exists()
    assert absolute == absolute_target
    assert absolute.exists()


class DummyProvider(LLMProvider):
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        return LLMResponse(content="ok")

    def get_default_model(self) -> str:
        return "dummy/model"


async def test_isolation_forces_workspace_restriction(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    exec_cfg = ExecToolConfig()
    exec_cfg.isolation.enabled = True
    exec_cfg.isolation.force_workspace_restriction = True
    exec_cfg.isolation.fail_closed = False

    responder = LLMResponder(
        bus=MessageBus(),
        provider=DummyProvider(),
        workspace=workspace,
        exec_config=exec_cfg,
        restrict_to_workspace=False,
    )

    assert responder.effective_restrict_to_workspace is True

    read_tool = responder.tools.get("read_file")
    assert read_tool is not None
    result = await read_tool.execute(str(outside))
    assert "outside allowed directory" in result

    await responder.aclose()


class FakeSandboxSession:
    counter = 0
    by_key: dict[str, "FakeSandboxSession"] = {}

    def __init__(self, session_key: str, workspace: Path):
        type(self).counter += 1
        self.instance_id = type(self).counter
        self.session_key = session_key
        self.workspace = workspace
        self.last_used_at = time.monotonic()
        self.active_since: float | None = None
        self._preempt_reason: str | None = None
        self._hold = asyncio.Event()
        self._stopped = False
        type(self).by_key[session_key] = self

    @property
    def active(self) -> bool:
        return self.active_since is not None

    async def start(self) -> None:
        return None

    async def run_command(self, command: str, cwd: str, timeout: int) -> CommandResult:
        if self._preempt_reason:
            raise SandboxPreemptedError(self._preempt_reason)

        self.active_since = time.monotonic()
        self.last_used_at = self.active_since
        try:
            if command == "hold":
                await self._hold.wait()
                if self._preempt_reason:
                    raise SandboxPreemptedError(self._preempt_reason)
                return CommandResult(output="held", exit_code=0)
            if command == "timeout":
                raise SandboxTimeoutError("timeout")
            return CommandResult(output=f"{self.session_key}:{cwd}", exit_code=0)
        finally:
            self.active_since = None
            self.last_used_at = time.monotonic()

    async def preempt(self, reason: str) -> None:
        self._preempt_reason = reason
        self._hold.set()
        await self.stop(reason=reason)

    async def stop(self, reason: str | None = None) -> None:
        self._stopped = True
        self._hold.set()

    def stop_now(self) -> None:
        self._stopped = True
        self._hold.set()


def _build_fake_manager(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ExecSandboxManager:
    FakeSandboxSession.counter = 0
    FakeSandboxSession.by_key = {}

    allowlist = MountAllowlist(allowed_roots=[tmp_path.resolve()], blocked_patterns=[])
    monkeypatch.setattr(
        "nanobot.agent.tools.exec_isolation.ExecSandboxManager._check_runtime",
        staticmethod(lambda: None),
    )
    monkeypatch.setattr(
        "nanobot.agent.tools.exec_isolation.MountAllowlist.load",
        staticmethod(lambda _path: allowlist),
    )
    monkeypatch.setattr("nanobot.agent.tools.exec_isolation.BubblewrapSandboxSession", FakeSandboxSession)

    return ExecSandboxManager(
        workspace=tmp_path / "workspace",
        max_containers=2,
        idle_seconds=600,
        pressure_policy="preempt_oldest_active",
        allowlist_path=tmp_path / "allowlist.json",
    )


async def test_manager_reuses_session_within_idle_window(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = _build_fake_manager(monkeypatch, tmp_path)

    r1 = await manager.execute("s1", "echo", str(workspace), timeout=3)
    assert "s1" in r1.output
    first = manager._sessions["s1"]

    await manager.execute("s1", "echo", str(workspace), timeout=3)
    second = manager._sessions["s1"]
    assert first is second

    await manager.aclose()


async def test_manager_rotates_session_after_idle_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = _build_fake_manager(monkeypatch, tmp_path)

    await manager.execute("s1", "echo", str(workspace), timeout=3)
    first = manager._sessions["s1"]
    first.last_used_at -= manager.idle_seconds + 1

    await manager.execute("s1", "echo", str(workspace), timeout=3)
    second = manager._sessions["s1"]
    assert first is not second

    await manager.aclose()


async def test_manager_evicts_idle_lru_at_capacity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = _build_fake_manager(monkeypatch, tmp_path)

    await manager.execute("s1", "echo", str(workspace), timeout=3)
    await manager.execute("s2", "echo", str(workspace), timeout=3)
    manager._sessions["s1"].last_used_at -= 1000

    await manager.execute("s3", "echo", str(workspace), timeout=3)
    assert "s1" not in manager._sessions
    assert "s2" in manager._sessions
    assert "s3" in manager._sessions

    await manager.aclose()


async def test_manager_preempts_oldest_active_when_full(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = _build_fake_manager(monkeypatch, tmp_path)

    t1 = asyncio.create_task(manager.execute("s1", "hold", str(workspace), timeout=30))
    await asyncio.sleep(0.05)
    t2 = asyncio.create_task(manager.execute("s2", "hold", str(workspace), timeout=30))
    await asyncio.sleep(0.05)

    r3 = await manager.execute("s3", "echo", str(workspace), timeout=3)
    assert "s3" in r3.output

    with pytest.raises(SandboxPreemptedError):
        await t1

    fake_s2 = FakeSandboxSession.by_key["s2"]
    fake_s2._hold.set()
    await t2

    await manager.aclose()


async def test_manager_drops_broken_session_after_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = _build_fake_manager(monkeypatch, tmp_path)

    with pytest.raises(SandboxTimeoutError):
        await manager.execute("s1", "timeout", str(workspace), timeout=3)
    assert "s1" not in manager._sessions

    await manager.execute("s1", "echo", str(workspace), timeout=3)
    assert "s1" in manager._sessions

    await manager.aclose()


async def test_bubblewrap_smoke_if_available(tmp_path: Path) -> None:
    if platform.system() != "Linux" or shutil.which("bwrap") is None or os.geteuid() == 0:
        pytest.skip("requires non-root Linux with bubblewrap")

    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)

    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text(
        '{"allowedRoots": ["%s"], "blockedHostPatterns": []}' % str(tmp_path),
        encoding="utf-8",
    )

    manager = ExecSandboxManager(
        workspace=workspace,
        max_containers=2,
        idle_seconds=60,
        pressure_policy="preempt_oldest_active",
        allowlist_path=allowlist,
    )

    result = await manager.execute("smoke", "pwd", str(workspace), timeout=5)
    assert "/workspace" in result.output
    await manager.aclose()
