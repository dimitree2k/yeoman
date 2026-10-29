"""Owner-private data stays out of group chats, for the owner too.

Related coverage elsewhere: ``history_read`` of another chat from a group is rejected
(test_history_secondary_readers.py), ``recall_conversation person=`` in a group needs
the owner DM (test_history_tools.py), and ``contacts list`` outside the owner DM is
refused (people/test_contacts_directory.py).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from yeoman_gateway.adapters.responder_llm import LLMResponder
from yeoman_gateway.agent import subagent as subagent_module
from yeoman_gateway.agent.subagent import SubagentManager
from yeoman_gateway.agent.tools.calendar import CalendarTool
from yeoman_gateway.agent.tools.file_access import FileAccessGrant, FileAccessResolver
from yeoman_gateway.agent.tools.ops import OpsTool
from yeoman_gateway.agent.tools.ops_manage import OpsManageTool
from yeoman_gateway.policy.engine import ActorContext, PolicyEngine
from yeoman_gateway.policy.schema import PolicyConfig
from yeoman_gateway.processing.tool_context import (
    ToolInvocationContext,
    is_group_chat,
    set_tool_context,
)

OWNER = "owner@s.whatsapp.net"
MEMBER = "member@s.whatsapp.net"
GROUP = "123@g.us"
REGISTERED = {
    "web_search", "web_fetch", "browse", "read_file", "list_dir", "cron",
    "calendar", "ops", "ops_manage", "exec", "spawn", "history_read",
}
GROUP_ALLOWLIST = {"web_search", "read_file", "list_dir", "cron"}
OWNER_TOOLS = {"web_fetch", "browse"}


@pytest.fixture
def tool_context():
    # Async tests run in their own copied Context, so a token from there cannot be reset
    # here; clearing the variable covers the sync tests that share this Context.
    def install(chat_id: str, *, is_owner: bool = True, channel: str = "whatsapp") -> None:
        set_tool_context(ToolInvocationContext(channel=channel, chat_id=chat_id, is_owner=is_owner))

    yield install
    set_tool_context(None)


# --- policy matrix ----------------------------------------------------------------


def _actor(sender: str, chat_id: str) -> ActorContext:
    return ActorContext(
        channel="whatsapp",
        chat_id=chat_id,
        sender_primary=sender,
        sender_aliases=[],
        is_group=chat_id.endswith("@g.us"),
        mentioned_bot=True,
        reply_to_bot=False,
    )


@pytest.mark.parametrize("sender", [OWNER, MEMBER])
@pytest.mark.parametrize("chat_id", [GROUP, OWNER, MEMBER])
def test_owner_gets_exactly_allowlist_plus_owner_tools(tmp_path: Path, sender, chat_id) -> None:
    policy = PolicyConfig.model_validate({
        "owners": {"whatsapp": [OWNER]},
        "ownerTools": sorted(OWNER_TOOLS),
        "defaults": {
            "allowedTools": {"mode": "allowlist", "tools": sorted(GROUP_ALLOWLIST)},
            "toolAccess": {"cron": {"mode": "owner_only"}},
        },
    })
    engine = PolicyEngine(policy, apply_channels={"whatsapp"}, workspace=tmp_path)

    allowed = set(engine.evaluate(_actor(sender, chat_id), REGISTERED).allowed_tools)

    if sender == OWNER:
        assert allowed == GROUP_ALLOWLIST | OWNER_TOOLS
    else:
        assert allowed == GROUP_ALLOWLIST - {"cron"}
    assert not allowed & {"calendar", "ops", "ops_manage", "exec", "spawn", "history_read"}


# --- owner-private tools refuse in groups ----------------------------------------


def _owner_private_tools() -> list:
    return [CalendarTool(MagicMock()), OpsTool(), OpsManageTool()]


@pytest.mark.parametrize("is_owner", [True, False])
@pytest.mark.parametrize("channel,chat_id", [("whatsapp", GROUP), ("telegram", "-100200")])
async def test_owner_private_tools_refuse_in_groups(tool_context, is_owner, channel, chat_id):
    tool_context(chat_id, is_owner=is_owner, channel=channel)
    for tool in _owner_private_tools():
        result = await tool.execute(action="list_calendars" if tool.name == "calendar" else "")
        assert result == (
            f"Error: {tool.name} is only available in the owner DM, not in group chats.")


async def test_owner_private_tools_still_run_in_the_owner_dm(tool_context):
    tool_context(OWNER)
    for tool in _owner_private_tools():
        # An unknown action reaches the tool's own dispatch, past the group gate.
        result = await tool.execute(action="no_such_action")
        assert "owner DM" not in result


def test_group_chat_detection() -> None:
    assert is_group_chat("whatsapp", GROUP)
    assert is_group_chat("telegram", "-100200")
    assert not is_group_chat("whatsapp", OWNER)
    assert not is_group_chat("whatsapp", "123@lid")
    assert not is_group_chat("telegram", "4711")
    assert not is_group_chat("cli", "direct")


# --- file-access grants -----------------------------------------------------------


def _resolver(tmp_path: Path, *, owner_only: bool = True) -> FileAccessResolver:
    granted = tmp_path / "private"
    granted.mkdir(exist_ok=True)
    grant = FileAccessGrant(id="notes", path=granted, recursive=True, mode="read")
    return FileAccessResolver(tmp_path / "workspace", grants=[grant], owner_only=owner_only)


@pytest.mark.parametrize("owner_only", [True, False])
@pytest.mark.parametrize("chat_id,is_owner,expected", [
    (OWNER, True, True),
    (OWNER, False, None),  # owner_only decides
    (GROUP, True, False),
    (GROUP, False, False),
])
def test_grants_never_enabled_in_groups(tmp_path, tool_context, owner_only, chat_id, is_owner, expected):
    tool_context(chat_id, is_owner=is_owner)
    responder = SimpleNamespace(file_access_resolver=_resolver(tmp_path, owner_only=owner_only))

    enabled = LLMResponder._should_enable_grants(responder, is_owner)

    assert enabled is (not owner_only if expected is None else expected)


class _StopAfterExecToolError(Exception):
    pass


@pytest.mark.parametrize("chat_id,is_owner,mounted", [
    (OWNER, True, True),
    (GROUP, True, False),
    (GROUP, False, False),
    (MEMBER, False, False),
])
async def test_subagent_mounts_grants_only_for_owner_outside_groups(
    tmp_path, tool_context, monkeypatch, chat_id, is_owner, mounted,
):
    captured: dict = {}

    def fake_exec_tool(**kwargs):
        captured.update(kwargs)
        raise _StopAfterExecToolError

    monkeypatch.setattr(subagent_module, "ExecTool", fake_exec_tool)
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    bus = MagicMock()
    bus.publish_inbound = AsyncMock()
    manager = SubagentManager(
        provider=provider, workspace=tmp_path, bus=bus,
        file_access_resolver=_resolver(tmp_path),
    )
    tool_context(chat_id, is_owner=is_owner)

    await manager._run_subagent("t1", "task", "label", {"channel": "whatsapp", "chat_id": chat_id})

    assert bool(captured["extra_mounts"]) is mounted
    assert bool(captured["grant_container_prefixes"]) is mounted


# --- exec sandbox grant mounts ----------------------------------------------------


class _RecordingSandboxSession:
    def __init__(self, session_key, workspace, extra_mounts=None):
        self.session_key = session_key
        self.extra_mounts = list(extra_mounts or [])
        self.last_used_at = 0.0
        self.active = False

    async def start(self) -> None:
        return None

    async def run_command(self, command, cwd, timeout):
        from yeoman_gateway.agent.tools.exec_isolation import CommandResult

        return CommandResult(output=f"{self.session_key}:{len(self.extra_mounts)}", exit_code=0)

    async def stop(self, reason=None) -> None:
        return None

    def stop_now(self) -> None:
        return None


@pytest.mark.parametrize("grants_on", [True, False])
async def test_exec_sandbox_mounts_grants_only_while_grants_are_active(
    tmp_path, monkeypatch, grants_on,
):
    from yeoman_gateway.agent.tools import exec_isolation
    from yeoman_gateway.agent.tools.exec_isolation import (
        ExecSandboxManager,
        MountAllowlist,
        SandboxMount,
    )
    from yeoman_gateway.agent.tools.file_access import enable_grants
    from yeoman_gateway.agent.tools.shell import ExecTool
    from yeoman_shared.config.schema import ExecIsolationConfig

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    granted = tmp_path / "private"
    granted.mkdir()
    allowlist = MountAllowlist(allowed_roots=[tmp_path.resolve()], blocked_patterns=[])
    monkeypatch.setattr(ExecSandboxManager, "_check_runtime", staticmethod(lambda: None))
    monkeypatch.setattr(MountAllowlist, "load", staticmethod(lambda _path: allowlist))
    monkeypatch.setattr(exec_isolation, "BubblewrapSandboxSession", _RecordingSandboxSession)
    tool = ExecTool(
        working_dir=str(workspace),
        isolation_config=ExecIsolationConfig(enabled=True, allowlist_path=str(tmp_path / "a.json")),
        extra_mounts=[SandboxMount(host_path=granted, container_path="/grants/notes", readonly=True)],
        grant_container_prefixes=["/grants/notes"],
    )
    tool.set_session_context("whatsapp:123@g.us")

    if grants_on:
        with enable_grants():
            output = await tool.execute(command="ls")
    else:
        output = await tool.execute(command="ls")

    sessions = tool._sandbox_manager._sessions
    if grants_on:
        assert output == "whatsapp:123@g.us:1"
        assert set(sessions) == {"whatsapp:123@g.us"}
    else:
        # A separate session that never had the grant folders mounted.
        assert output == "whatsapp:123@g.us#no-grants:0"
        assert set(sessions) == {"whatsapp:123@g.us#no-grants"}
