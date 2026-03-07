# tests/test_session_tool_traces.py
import pytest
from yeoman.session.manager import Session


def test_session_stores_tool_call():
    session = Session(key="test:chat1")
    session.add_message("user", "What's the weather?")
    session.add_tool_call(
        tool_name="web_search",
        tool_call_id="tc_123",
        arguments={"query": "weather Berlin"},
        result="Sunny, 22C",
    )
    session.add_message("assistant", "It's sunny and 22C in Berlin.")

    history = session.get_history()
    assert len(history) == 2  # tool calls excluded from LLM history

    full = session.get_full_history()
    assert len(full) == 3
    assert full[1]["role"] == "tool_trace"
    assert full[1]["tool_name"] == "web_search"
