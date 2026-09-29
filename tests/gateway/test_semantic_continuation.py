import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from yeoman_gateway.processing.continuation import ContinuationJudge


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
