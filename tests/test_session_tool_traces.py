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


def test_get_history_skips_legacy_rows_without_content():
    session = Session(key="test:chat2")
    session.messages = [
        {"role": "user", "content": "hi", "timestamp": "2026-03-07T00:00:00"},
        {
            "role": "tool_trace",
            "tool_name": "web_search",
            "tool_call_id": "tc_legacy",
            "arguments": {"query": "x"},
            "result": "y",
            "timestamp": "2026-03-07T00:00:01",
        },
        {"role": "assistant", "content": "hello", "timestamp": "2026-03-07T00:00:02"},
        {"role": "assistant", "timestamp": "2026-03-07T00:00:03"},
    ]

    history = session.get_history()
    assert history == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
