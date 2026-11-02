"""The reply prompt appends one ``[Group State]`` block and nothing else."""

from __future__ import annotations

from pathlib import Path

from yeoman_gateway.agent.context import ContextBuilder

CONVERSATION_STATE = {
    "addressed_to_bot": True,
    "address_mode": "direct",
    "preferred_action": "answer",
    "answer_shape": "short_take",
    "room_mode": "group",
}
BLOCK = "[Group State] synthetic view"


def _builder(tmp_path: Path) -> ContextBuilder:
    return ContextBuilder(tmp_path)


def test_context_without_block_is_byte_identical(tmp_path: Path) -> None:
    builder = _builder(tmp_path)
    plain = {"conversation_state": dict(CONVERSATION_STATE)}

    without = builder._build_user_content("hello", None, dict(plain))
    empty = builder._build_user_content("hello", None, {**plain, "group_state_block": ""})
    none = builder._build_user_content("hello", None, {**plain, "group_state_block": None})
    non_string = builder._build_user_content("hello", None, {**plain, "group_state_block": 7})

    assert empty == without
    assert none == without
    assert non_string == without


def test_context_block_after_conversation_state(tmp_path: Path) -> None:
    builder = _builder(tmp_path)
    metadata = {
        "conversation_state": dict(CONVERSATION_STATE),
        "group_state_block": BLOCK,
    }

    rendered = builder._build_user_content("hello", None, metadata)

    assert rendered.endswith(f"\n\n{BLOCK}")
    assert rendered.index("[Conversation State]") < rendered.index(BLOCK)
