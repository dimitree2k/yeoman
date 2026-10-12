from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from yeoman_gateway.adapters.responder_llm import LLMResponder
from yeoman_gateway.cli import agent_commands


@pytest.mark.asyncio
async def test_agent_turn_uses_single_cli_intent_and_reads_nested_response(monkeypatch) -> None:
    sent: list[bytes] = []
    closed: list[bool] = []

    class Reader:
        async def readline(self) -> bytes:
            return b'{"status":"ok","response":{"response":"ready"}}\n'

    class Writer:
        def write(self, data: bytes) -> None:
            sent.append(data)

        async def drain(self) -> None:
            pass

        def close(self) -> None:
            closed.append(True)

        async def wait_closed(self) -> None:
            pass

    async def connect(path: str, *, limit: int):
        assert path == "/tmp/gateway.sock"
        assert limit == agent_commands.MAX_IPC_REQUEST_BYTES
        return Reader(), Writer()

    monkeypatch.setattr(agent_commands.asyncio, "open_unix_connection", connect)

    result = await agent_commands._request_agent_turn(
        Path("/tmp/gateway.sock"), "summarize", "cli:notes"
    )

    assert result == "ready"
    assert len(sent) == 1
    assert json.loads(sent[0]) == {
        "cmd": "trigger_agent_turn",
        "args": {
            "prompt": "summarize",
            "session_key": "cli:notes",
            "channel": "cli",
            "chat_id": "direct",
        },
    }
    assert closed == [True]


@pytest.mark.asyncio
async def test_process_direct_preserves_explicit_empty_tool_allowlist(tmp_path) -> None:
    from tests.gateway.history_test_helpers import ProjectedHistory

    responder = LLMResponder.__new__(LLMResponder)
    responder.tools = SimpleNamespace(tool_names=("write_file",))
    responder._generate = AsyncMock(return_value="ready")
    history = ProjectedHistory(tmp_path / "cli-history")
    history.wire(responder)
    try:
        await responder.process_direct("hello", allowed_tools=set())
    finally:
        history.close()

    assert responder._generate.await_args.kwargs["allowed_tools"] == set()
