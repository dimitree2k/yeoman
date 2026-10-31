"""Nightly reception labeler: strict JSON, untrusted wrapping, no stored text."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from yeoman_gateway.processing.model_route import RouteReply
from yeoman_gateway.reception.labeler import (
    LABEL_RESPONSE_FORMAT,
    LABEL_SYSTEM_PROMPT,
    PROMPT_VERSION,
    DayWindow,
    ReceptionLabelJob,
    ReceptionLabelRunner,
    parse_labels,
    strip_identifiers,
)
from yeoman_gateway.reception.store import SentimentStore

from tests.gateway.history_test_helpers import ProjectedHistory

CHAT = "120363000000000055@g.us"
DAY_START = int(datetime(2026, 9, 29, tzinfo=UTC).timestamp())
DAY = DayWindow("2026-09-29", DAY_START * 1000, (DAY_START + 86400) * 1000)
PHONE = "4915100012345"


class FakeClient:
    model = "fake/labeler"
    structured_output = "json_schema_strict"

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.calls: list[list[dict[str, str]]] = []

    async def chat_with_usage(self, messages, **kwargs):  # noqa: ANN001, ANN003
        self.calls.append(messages)
        if self.answers:
            return RouteReply(content=self.answers.pop(0), model=self.model)
        payload = json.loads(messages[1]["content"].split("\n", 1)[1])
        labels = []
        for message in payload["messages"]:
            for candidate in message["candidates"]:
                labels.append({
                    "arvid_message_id": message["arvid_message_id"],
                    "message_id": candidate["id"],
                    "responds_to_arvid": "yes",
                    "stance": "playful",
                    "signals": ["uses_his_answer"],
                    "confidence": 0.9,
                })
        return RouteReply(content=json.dumps({"labels": labels}), model=self.model)


def _history(tmp_path: Path) -> ProjectedHistory:
    history = ProjectedHistory(tmp_path / "history")
    base = DAY_START + 9 * 3600
    history.add_message(
        chat_id=CHAT, message_id="in-1", text=f"frage an arvid {PHONE}", timestamp=base - 60,
        sender_id=f"{PHONE}@s.whatsapp.net",
    )
    history.add_message(
        chat_id=CHAT, message_id="out-1", text="eine synthetische antwort",
        timestamp=base, direction="out", reply_to_message_id="in-1",
    )
    history.add_message(
        chat_id=CHAT, message_id="in-2", text="danke, das hilft",
        timestamp=base + 30, sender_id=f"{PHONE}@s.whatsapp.net", reply_to_message_id="out-1",
    )
    return history


def _runner(tmp_path: Path, history: ProjectedHistory, client: FakeClient) -> ReceptionLabelRunner:
    return ReceptionLabelRunner(
        store=SentimentStore(tmp_path / "ops" / "sentiment.db"),
        history=history, client_factory=lambda: client, batch_messages=5,
    )


def test_parse_labels_keeps_only_valid_items() -> None:
    raw = json.dumps({"labels": [
        {"arvid_message_id": "a1", "message_id": "m1", "responds_to_arvid": "yes",
         "stance": "playful", "signals": ["uses_his_answer", "made_up"], "confidence": 0.8},
        {"arvid_message_id": "unknown", "message_id": "m1", "responds_to_arvid": "yes",
         "stance": "playful", "signals": [], "confidence": 0.8},
        {"arvid_message_id": "a1", "message_id": "m1", "responds_to_arvid": "maybe",
         "stance": "playful", "signals": [], "confidence": 0.8},
        {"arvid_message_id": "a1", "message_id": "m1", "responds_to_arvid": "no",
         "stance": "neutral", "signals": [], "confidence": 7},
    ]})
    parsed = parse_labels(raw, arvid_ids={"a1": "actual-a"}, message_ids={"m1": "actual-m"})
    assert parsed is not None and len(parsed) == 1
    assert parsed[0].signals == ("uses_his_answer",)
    assert parse_labels("not json", arvid_ids={"a1": "x"}, message_ids={"m1": "y"}) is None
    assert parse_labels("[]", arvid_ids={}, message_ids={}) is None
    crossed = json.dumps({"labels": [
        {"arvid_message_id": "a2", "message_id": "m1", "responds_to_arvid": "yes",
         "stance": "neutral", "signals": [], "confidence": 0.5},
    ]})
    assert parse_labels(
        crossed, arvid_ids={"a1": "a", "a2": "b"}, message_ids={"m1": "m"},
        pairs={"a1": frozenset({"m1"})},
    ) == []


def test_prompt_names_every_key_the_validator_requires() -> None:
    # json_object and prompt_only modes see no schema: the prompt alone must name the keys.
    item = LABEL_RESPONSE_FORMAT["json_schema"]["schema"]["properties"]["labels"]["items"]
    for key in item["required"]:
        assert key in LABEL_SYSTEM_PROMPT


def test_label_without_arvid_message_id_is_dropped() -> None:
    raw = json.dumps({"labels": [
        {"id": "m1", "responds_to_arvid": "yes", "stance": "playful",
         "signals": [], "confidence": 0.8},
    ]})
    assert parse_labels(raw, arvid_ids={"a1": "a"}, message_ids={"m1": "m"}) == []


def test_identifiers_are_stripped_before_the_model_sees_text() -> None:
    assert PHONE not in strip_identifiers(f"call {PHONE} or 4915100012345@s.whatsapp.net")
    assert "@[id]" in strip_identifiers(f"mention @{PHONE}")


@pytest.mark.asyncio
async def test_label_day_wraps_text_stores_no_text_and_is_idempotent(tmp_path: Path) -> None:
    history = _history(tmp_path)
    client = FakeClient()
    runner = _runner(tmp_path, history, client)
    try:
        outcome = await runner.label_day(CHAT, DAY)
        assert outcome.state == "done" and outcome.labels >= 1
        prompt = client.calls[0][1]["content"]
        assert "--- UNTRUSTED EXTERNAL CONTENT ---" in prompt
        assert "never follow" in prompt.lower()
        assert PHONE not in prompt
        assert LABEL_SYSTEM_PROMPT in client.calls[0][0]["content"]
        labels = runner._store.labels(CHAT, iso_week="2026-W40")
        assert labels and labels[0].prompt_version == PROMPT_VERSION
        assert labels[0].stance == "playful"
        assert b"eine synthetische antwort" not in runner._store.path.read_bytes()
        # A second pass for the same day makes no model call and stores nothing new.
        again = await runner.label_day(CHAT, DAY)
        assert (again.state, again.labels, len(client.calls)) == ("done", 0, 1)
    finally:
        history.close()
        runner._store.close()


@pytest.mark.asyncio
async def test_alert_is_claimed_at_most_once_per_day(tmp_path: Path) -> None:
    history = _history(tmp_path)
    runner = _runner(tmp_path, history, FakeClient())
    sent: list[tuple[str, str]] = []

    async def sender(chat_id: str, reason: str) -> None:
        sent.append((chat_id, reason))

    job = ReceptionLabelJob(runner=runner, chats=lambda: (CHAT,), alert_sender=sender)
    try:
        runner.store.store_label(
            chat_id=CHAT, iso_week="2026-W40", arvid_message_id="a", message_id="m",
            responds_to_arvid="yes", stance="annoyed",
            signals=("asks_to_remove_or_mute",), confidence=0.9, model="fake",
            prompt_version="v1", labeled_at_ms=DAY.start_ms,
        )
        await job._maybe_alert(CHAT, DAY, DAY.start_ms + 1000)
        await job._maybe_alert(CHAT, DAY, DAY.start_ms + 2000)
        assert sent == [(CHAT, "asks_to_remove_or_mute")]
    finally:
        history.close()
        runner.store.close()


@pytest.mark.asyncio
async def test_invalid_json_is_bounded_and_stores_nothing(tmp_path: Path) -> None:
    history = _history(tmp_path)
    client = FakeClient("not json", "still not json")
    runner = _runner(tmp_path, history, client)
    try:
        outcome = await runner.label_day(CHAT, DAY)
        assert (outcome.state, outcome.reason) == ("failed", "invalid_output")
        assert len(client.calls) == 2
        assert runner._store.labels(CHAT) == ()
    finally:
        history.close()
        runner._store.close()


class ProviderErrorClient(FakeClient):
    """The provider wrapper reports a rejected request as an empty error reply."""

    async def chat_with_usage(self, messages, **kwargs):  # noqa: ANN001, ANN003
        self.calls.append(messages)
        return RouteReply(content="", model=self.model, finish_reason="error")


@pytest.mark.asyncio
async def test_provider_error_reply_is_not_reported_as_invalid_json(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from loguru import logger

    history = _history(tmp_path)
    client = ProviderErrorClient()
    runner = _runner(tmp_path, history, client)
    handler = logger.add(caplog.handler, format="{message}", level="WARNING")
    try:
        outcome = await runner.label_day(CHAT, DAY)
        assert (outcome.state, outcome.reason) == ("failed", "provider_error")
        assert len(client.calls) == 2
        assert "reception_label_provider_failed" in caplog.text
        assert "reception_label_invalid_json" not in caplog.text
        assert runner._store.labels(CHAT) == ()
    finally:
        logger.remove(handler)
        history.close()
        runner._store.close()


@pytest.mark.asyncio
async def test_route_unavailable_is_a_clean_failure(tmp_path: Path) -> None:
    from yeoman_gateway.processing.model_route import RouteUnavailableError

    history = _history(tmp_path)
    runner = ReceptionLabelRunner(
        store=SentimentStore(tmp_path / "ops" / "sentiment.db"), history=history,
        client_factory=lambda: (_ for _ in ()).throw(RouteUnavailableError("missing")),
    )
    try:
        outcome = await runner.label_day(CHAT, DAY)
        assert (outcome.state, outcome.reason) == ("failed", "route_unavailable")
    finally:
        history.close()
        runner._store.close()
