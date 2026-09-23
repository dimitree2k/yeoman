from __future__ import annotations

import asyncio

import pytest
from yeoman_gateway.processing.model_route import RouteReply
from yeoman_gateway.short_reply.decider import ShortReplyDecider, ShortReplyInput

VOCAB = ("👍", "🤙", "🙏", "😄", "😂", "😎", "💀", "🔥", "👀")


class _Client:
    def __init__(
        self,
        content: str = "",
        *,
        usage=None,
        delay: float = 0.0,
        error=None,
        finish_reason: str = "stop",
    ):
        self.content = content
        self.usage = usage or {"prompt_tokens": 150, "completion_tokens": 12}
        self.delay = delay
        self.error = error
        self.finish_reason = finish_reason
        self.calls: list[dict[str, object]] = []

    async def chat_with_usage(
        self, messages, *, max_tokens, response_format=None, max_retries=None
    ):
        self.calls.append(
            {
                "messages": messages,
                "max_tokens": max_tokens,
                "response_format": response_format,
                "max_retries": max_retries,
            }
        )
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return RouteReply(
            content=self.content,
            usage=self.usage,
            model="m",
            latency_ms=5,
            finish_reason=self.finish_reason,
        )


def _decider(client: _Client, **kwargs) -> ShortReplyDecider:
    return ShortReplyDecider(client=client, allowed_emojis=VOCAB, **kwargs)


async def test_a_reaction_verdict_keeps_ranked_approved_emojis_and_usage() -> None:
    client = _Client(
        '{"action":"react","emojis":["😎","🤖","😄","😎","🔥","🙏"],"confidence":0.8}'
    )
    verdict = await _decider(client).decide(
        ShortReplyInput(
            text="Jepp, hatte Glück 😊", bot_text="Starker Trade.", recent_emojis=("👍",)
        )
    )
    assert verdict.action == "react"
    assert verdict.emojis == ("😎", "😄", "🔥")
    assert verdict.usage == {"prompt_tokens": 150, "completion_tokens": 12}
    assert verdict.error == ""


async def test_the_prompt_carries_vocabulary_recent_emojis_and_both_texts() -> None:
    client = _Client('{"action":"none","emojis":[],"confidence":0.5}')
    await _decider(client, max_output_tokens=64).decide(
        ShortReplyInput(text="ありがとう！", bot_text="Gern.", recent_emojis=("🙏", "😂"))
    )
    call = client.calls[0]
    system = call["messages"][0]["content"]
    user = call["messages"][1]["content"]
    assert "any language" in system
    assert "🙏 😂" in system and "👍 🤙" in system
    assert "Gern." in user and "ありがとう！" in user
    assert call["max_tokens"] == 64
    assert call["response_format"] == {"type": "json_object"}
    assert call["max_retries"] == 0


async def test_answer_and_none_are_passed_through() -> None:
    answer = await _decider(_Client('{"action":"answer","confidence":0.9}')).decide(
        ShortReplyInput(text="Es gab nichts dergleichen, es war buy and hold")
    )
    none = await _decider(_Client('{"action":"none"}')).decide(ShortReplyInput(text="k"))
    assert answer.action == "answer" and none.action == "none"


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("answer", "answer"),
        ("antwort", "answer"),
        ("reply", "answer"),
        ("comment", "answer"),
        ("react", "react"),
        ("reaction", "react"),
        ("reagieren", "react"),
        ("none", "none"),
        ("silence", "none"),
        ("silent", "none"),
        ("no", "none"),
        ("false", "none"),
        ("schweigen", "none"),
    ],
)
async def test_legacy_action_aliases_remain_accepted(token: str, expected: str) -> None:
    verdict = await _decider(
        _Client(f'{{"action":"{token}","emojis":["😂"]}}')
    ).decide(ShortReplyInput(text="ok"))
    assert verdict.action == expected


async def test_fenced_json_and_a_single_emoji_key_are_accepted() -> None:
    verdict = await _decider(_Client('```json\n{"action":"react","emoji":"😂"}\n```')).decide(
        ShortReplyInput(text="lol")
    )
    assert verdict.action == "react" and verdict.emojis == ("😂",)


@pytest.mark.parametrize(
    ("content", "error"),
    [
        ("", "invalid_json"),
        ('{"action":"react","emojis":["🤖"]}', "no_valid_emoji"),
        ('{"action":"maybe"}', "invalid_action"),
        ('["react"]', "invalid_json"),
        ('{"action":"react","emo', "invalid_json"),
    ],
)
async def test_unusable_answers_are_errors_with_usage(content: str, error: str) -> None:
    verdict = await _decider(_Client(content)).decide(ShortReplyInput(text="ok"))
    assert verdict.action == "error" and verdict.error == error
    assert verdict.usage.get("prompt_tokens") == 150


async def test_timeouts_and_provider_errors_are_errors() -> None:
    slow = await _decider(_Client("{}", delay=0.2), timeout_seconds=0.01).decide(
        ShortReplyInput(text="ok")
    )
    broken = await _decider(_Client(error=RuntimeError("boom"))).decide(
        ShortReplyInput(text="ok")
    )
    assert slow.error == "timeout" and broken.error == "provider_error"


async def test_provider_error_and_token_truncation_are_not_invalid_json() -> None:
    provider = await _decider(_Client("", finish_reason="error")).decide(
        ShortReplyInput(text="ok")
    )
    truncated = await _decider(_Client("{", finish_reason="length")).decide(
        ShortReplyInput(text="ok")
    )
    assert provider.error == "provider_error"
    assert truncated.error == "truncated_output"


async def test_cancellation_is_not_turned_into_silence() -> None:
    task = asyncio.create_task(
        _decider(_Client("{}", delay=1.0), timeout_seconds=5).decide(ShortReplyInput(text="ok"))
    )
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_empty_input_or_vocabulary_never_calls_the_model() -> None:
    client = _Client('{"action":"none"}')
    empty = await _decider(client).decide(ShortReplyInput(text="  "))
    no_vocab = await ShortReplyDecider(client=client, allowed_emojis=()).decide(
        ShortReplyInput(text="ok")
    )
    assert empty.error == "empty_input" and no_vocab.error == "no_vocabulary"
    assert client.calls == []


async def test_a_sticker_without_text_is_decided_from_its_media_text() -> None:
    client = _Client('{"action":"react","emojis":["😂"]}')
    verdict = await _decider(client).decide(ShortReplyInput(text="", media_text="sticker"))
    assert verdict.action == "react"
    assert "[media: sticker]" in client.calls[0]["messages"][1]["content"]


async def test_long_texts_are_truncated_by_grapheme() -> None:
    client = _Client('{"action":"none"}')
    await _decider(client).decide(ShortReplyInput(text="x" * 1000, bot_text="y" * 1000))
    user = client.calls[0]["messages"][1]["content"]
    assert "x" * 400 in user and "x" * 401 not in user
    assert "y" * 400 in user and "y" * 401 not in user
