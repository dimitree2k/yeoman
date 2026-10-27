"""Semantic reopen: embeddings shortlist, the verifier confirms, anything else abstains."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from yeoman_gateway.processing.store import ProcessingStore
from yeoman_gateway.processing.topic_reopen import TopicReopener, TopicVerifier

CHAT = "chat@g.us"
#: Hand-made unit vectors per topic; the fake embedder picks one by the text's first word.
AXES = {"car": [1.0, 0.0, 0.0], "pledge": [0.0, 1.0, 0.0], "lights": [0.95, 0.1, 0.0],
        "weather": [0.0, 0.0, 1.0]}


class _Embedder:
    def __init__(self) -> None:
        self.batches: list[list[str]] = []

    async def __call__(self, texts: list[str]) -> list[list[float]]:
        self.batches.append(list(texts))
        return [AXES[text.split()[0]] for text in texts]


class _Verifier:
    def __init__(self, answer: str | None = "first") -> None:
        self.answer = answer
        self.seen: list[tuple[str, ...]] = []

    async def choose(self, *, text, candidates):
        self.seen.append(tuple(thread_id for thread_id, _label, _at in candidates))
        return candidates[0][0] if self.answer == "first" else self.answer


def _reopener(pool, verifier, embedder=None, **kwargs):
    return TopicReopener(candidates=lambda channel, chat_id: pool, embed=embedder or _Embedder(),
                         verifier=verifier, **kwargs)


POOL = (("th-pledge", "pledge Giving Pledge Buffett", 2), ("th-car", "car Alex new car", 1))


@pytest.mark.asyncio
async def test_verified_shortlist_reopens_the_matching_thread():
    verifier = _Verifier()

    verdict = await _reopener(POOL, verifier).resolve(channel="whatsapp", chat_id=CHAT,
                                                      text="lights on Alex car?")

    assert verdict.thread_id == "th-car" and verdict.reason == "verified"
    assert verifier.seen == [("th-car",)]


@pytest.mark.asyncio
async def test_nothing_close_enough_never_asks_the_verifier():
    verifier = _Verifier()

    verdict = await _reopener(POOL, verifier).resolve(channel="whatsapp", chat_id=CHAT,
                                                      text="weather tomorrow?")

    assert verdict.thread_id is None and verdict.reason == "below_threshold"
    assert verifier.seen == []


@pytest.mark.asyncio
async def test_verifier_abstention_and_errors_start_a_new_thread():
    abstains = await _reopener(POOL, _Verifier(None)).resolve(
        channel="whatsapp", chat_id=CHAT, text="car again")

    class _Broken:
        async def __call__(self, texts):
            raise RuntimeError("provider down")

    broken = await _reopener(POOL, _Verifier(), _Broken()).resolve(
        channel="whatsapp", chat_id=CHAT, text="car again")

    assert (abstains.thread_id, abstains.reason) == (None, "verifier_abstained")
    assert (broken.thread_id, broken.reason) == (None, "error")


@pytest.mark.asyncio
async def test_topic_vectors_are_cached_between_requests():
    embedder = _Embedder()
    reopener = _reopener(POOL, _Verifier(), embedder)

    await reopener.resolve(channel="whatsapp", chat_id=CHAT, text="car one")
    await reopener.resolve(channel="whatsapp", chat_id=CHAT, text="car two")

    assert embedder.batches[1] == ["car two"]


@pytest.mark.asyncio
async def test_verifier_may_only_return_a_supplied_thread():
    class _Client:
        def __init__(self, content, finish="stop"):
            self.reply = SimpleNamespace(content=content, finish_reason=finish)

        async def chat_with_usage(self, messages, **kwargs):
            assert "untrusted" in messages[0]["content"]
            return self.reply

    candidates = (("th-car", "car", 1),)

    async def ask(client):
        return await TopicVerifier(client=client).choose(text="car?", candidates=candidates)

    assert await ask(_Client(json.dumps({"thread_id": "th-car"}))) == "th-car"
    assert await ask(_Client(json.dumps({"thread_id": "th-other"}))) is None
    assert await ask(_Client(json.dumps({"thread_id": "th-car"}), finish="length")) is None


def test_only_dormant_open_threads_of_the_chat_are_candidates(tmp_path: Path):
    store = ProcessingStore(tmp_path / "p.db")

    def thread(event_id, chat=CHAT, kind="group"):
        thread_id = store.open_thread(channel="whatsapp", chat_id=chat, root_principal="o",
                                      kind=kind, trigger_event_id=event_id, now_ms=1_000)
        store.note_thread_topic(thread_id, source_id=event_id, text=f"topic {event_id}",
                                now_ms=1_000)
        return thread_id

    dormant, active, closed = thread("d"), thread("a"), thread("c")
    elsewhere, ambient = thread("x", chat="other@g.us"), thread("amb", kind="ambient")
    assert store.mark_dormant((dormant, closed, elsewhere, ambient), 2_000, "test")
    with store._write() as conn:  # /new closes a thread; keep only the resulting state here
        conn.execute("UPDATE threads SET state = 'closed', close_reason = 'manual_new'"
                     " WHERE thread_id = ?", (closed,))

    assert [row[0] for row in store.reopen_candidates(channel="whatsapp", chat_id=CHAT)] == [dormant]
    assert active
    store.close()
