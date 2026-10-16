"""A busy batch must not lose every statement when the model output is cut off."""

from __future__ import annotations

import json
from types import SimpleNamespace

from yeoman_gateway.knowledge._capture_worker import MAX_CANDIDATES_PER_JOB, StatementExtractor
from yeoman_shared.config.schema import Config


class RecordedProvider:
    def __init__(self, content: str, finish_reason: str = "stop") -> None:
        self.content = content
        self.finish_reason = finish_reason
        self.calls: list[dict[str, object]] = []

    async def chat(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(content=self.content, finish_reason=self.finish_reason)


def _config() -> Config:
    return Config.model_validate(
        {
            "models": {
                "routes": {"memory.capture.extract": "recorded"},
                "profiles": {"recorded": {"kind": "chat", "model": "recorded/offline"}},
            }
        }
    )


def _row(index: int) -> dict[str, object]:
    return {
        "content": f"synthetic statement {index}",
        "source": index,
        "basis": "explicit_statement",
        "certainty": "asserted",
        "valid_until": None,
    }


def _items(count: int) -> list[SimpleNamespace]:
    return [SimpleNamespace(text=f"synthetic message {i}", revoked=False) for i in range(count)]


def test_truncated_output_keeps_the_complete_rows() -> None:
    full = json.dumps({"statements": [_row(i) for i in range(6)]})
    cut = full[: full.index('"synthetic statement 3"') + 10]
    provider = RecordedProvider(cut, finish_reason="length")
    drafts = StatementExtractor(config=_config(), provider=provider)(_items(6))

    assert [draft.content for draft in drafts] == [f"synthetic statement {i}" for i in range(3)]


def test_rows_wrapped_in_prose_are_still_read() -> None:
    body = json.dumps({"statements": [_row(0)]})
    provider = RecordedProvider(f"Here you go:\n{body}\nDone.")
    drafts = StatementExtractor(config=_config(), provider=provider)(_items(1))

    assert [draft.content for draft in drafts] == ["synthetic statement 0"]


def test_prompt_states_the_per_batch_limit() -> None:
    provider = RecordedProvider(json.dumps({"statements": []}))
    StatementExtractor(config=_config(), provider=provider)(_items(2))

    system = provider.calls[0]["messages"][0]["content"]  # type: ignore[index]
    assert f"at most {MAX_CANDIDATES_PER_JOB} statements" in system


def test_garbage_still_yields_nothing() -> None:
    provider = RecordedProvider("not json at all", finish_reason="length")

    assert StatementExtractor(config=_config(), provider=provider)(_items(1)) == []
