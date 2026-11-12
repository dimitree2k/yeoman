import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from yeoman_gateway.processing.continuation import ContinuationJudge, _quoted_someone_else_since


@pytest.mark.parametrize(("output", "finish", "expected"), [
    ('{"anchor_message_id":"bot-1"}', "stop", "bot-1"),
    ('{"anchor_message_id":null}', "stop", None),
    ('{"anchor_message_id":"other-chat"}', "stop", None),
    ('{"anchor_message_id":"bot-1"}', "length", None),
    ('not json', "stop", None),
    ('[]', "stop", None),
])
async def test_judge_can_only_select_supplied_anchor(output, finish, expected):
    client = SimpleNamespace(chat_with_usage=AsyncMock(
        return_value=SimpleNamespace(content=output, finish_reason=finish)))
    result = await ContinuationJudge(client=client).choose(
        text="295k. 330k loan", anchors=[{"id": "bot-1", "bot": "Price?"}], context=[],
    )
    assert result == expected
    assert client.chat_with_usage.call_args.kwargs["max_retries"] == 0


async def test_judge_timeout_is_not_continuity_but_cancellation_propagates():
    client = SimpleNamespace(chat_with_usage=AsyncMock(side_effect=TimeoutError()))
    judge = ContinuationJudge(client=client)
    assert await judge.choose(text="295k", anchors=[{"id": "bot-1"}], context=[]) is None
    client.chat_with_usage.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await judge.choose(text="295k", anchors=[{"id": "bot-1"}], context=[])


class _Queries:
    """Minimal history: rows by native id, ``window`` in time order."""

    def __init__(self, rows):
        self.rows = rows

    def window(self, *, chat_id, after_ms, before_ms, limit):
        return [row for row in self.rows if after_ms < row["sent_ms"] < before_ms][:limit]

    def native_message(self, *, chat_id, native_id):
        return next((row for row in self.rows if row["native_message_id"] == native_id), None)

    def assistant_contact_ids(self):
        return frozenset({"arvid"})


def _row(native, sender, sent_ms, reply_to=None):
    return {"native_message_id": native, "sender_contact_id": sender, "sent_ms": sent_ms,
            "reply_to_native_id": reply_to}


@pytest.mark.parametrize(
    ("later", "expected"),
    [
        ([], False),
        ([_row("f2", "frank", 30, "d1")], True),                      # quoted Dimi
        ([_row("f2", "frank", 30, "d1"), _row("f3", "frank", 40, "a1")], False),  # back to Arvid
        ([_row("f2", "frank", 30, "f1")], False),                     # quoted himself
        ([_row("d2", "dimi", 30, "f1")], False),                      # someone else quoted
        ([_row("f2", "frank", 30, "gone")], False),                   # unknown target
    ],
)
def test_sender_who_quoted_another_person_left_arvids_exchange(later, expected):
    rows = [_row("d1", "dimi", 5), _row("f1", "frank", 10), _row("a1", "arvid", 20, "f1"), *later]
    queries = _Queries(rows)
    assert _quoted_someone_else_since(
        queries, chat_id="g", sender="frank", after_ms=20, before_ms=100,
    ) is expected


def test_quote_check_needs_a_known_sender():
    queries = _Queries([_row("f2", "frank", 30, "d1"), _row("d1", "dimi", 5)])
    assert _quoted_someone_else_since(
        queries, chat_id="g", sender=None, after_ms=20, before_ms=100,
    ) is False


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("json_schema_strict", "schema"), ("json_object", {"type": "json_object"}),
     ("prompt_only", None)],
)
def test_schema_format_follows_the_route_profile(mode, expected):
    from yeoman_gateway.processing.model_route import schema_format_for

    schema = {"type": "json_schema", "json_schema": {"name": "x"}}
    result = schema_format_for(SimpleNamespace(structured_output=mode), schema)
    assert result == (schema if expected == "schema" else expected)


@pytest.mark.asyncio
async def test_json_object_route_gets_plain_json_and_ids_are_still_checked():
    reply = SimpleNamespace(content='{"anchor_message_id": "invented"}', finish_reason="stop")
    client = SimpleNamespace(structured_output="json_object",
                             chat_with_usage=AsyncMock(return_value=reply))
    judge = ContinuationJudge(client=client)
    assert await judge.choose(text="x", anchors=[{"id": "a1"}], context=[]) is None
    assert client.chat_with_usage.call_args.kwargs["response_format"] == {"type": "json_object"}
