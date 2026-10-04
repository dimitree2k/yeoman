"""Offline diagnosis of fields lost by the current statement extractor."""

from __future__ import annotations

import json
from collections.abc import Mapping
from types import SimpleNamespace

from yeoman_gateway.knowledge._capture import ObservedEvent
from yeoman_gateway.knowledge._capture_worker import StatementDraft, StatementExtractor
from yeoman_shared.config.schema import Config

from tests.gateway.capture_harness import AUTHOR, OTHER, CaptureHarness, Registry


class RecordedProvider:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[dict[str, object]] = []

    async def chat(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(content=self.content)


def _synthetic_config() -> Config:
    return Config.model_validate(
        {
            "models": {
                "routes": {"memory.capture.extract": "recorded"},
                "profiles": {"recorded": {"kind": "chat", "model": "recorded/offline"}},
            }
        }
    )


def test_diagnosis_detects_structured_fields_lost_by_current_parser() -> None:
    response_row = {
        "content": "A synthetic reported claim.",
        "source": 1,
        "basis": "reported_statement",
        "certainty": "asserted",
        "valid_until": None,
        "people": [{"person": "p1", "role": "subject"}],
        "attributes": [
            {
                "person": "p1",
                "key": "occupation",
                "value": "synthetic",
                "polarity": "positive",
            }
        ],
        "time_basis": "event",
        "time_precision": "day",
        "unresolved_mentions": ["p1"],
    }
    provider = RecordedProvider(json.dumps({"statements": [response_row]}))
    extractor = StatementExtractor(config=_synthetic_config(), provider=provider)
    drafts = extractor(
        [
            SimpleNamespace(text="revoked tuning source", revoked=True),
            SimpleNamespace(text="synthetic tuning source", revoked=False),
        ]
    )

    assert len(provider.calls) == 1
    assert "[1] synthetic tuning source" in str(provider.calls[0]["messages"])
    assert len(drafts) == 1
    draft = drafts[0]
    assert isinstance(draft, StatementDraft)
    assert draft.content == response_row["content"]
    assert draft.source_index == 1
    assert draft.people == ()
    assert draft.attributes == ()
    assert draft.time_basis == "unknown"
    assert draft.time_precision == "unknown"
    assert draft.unresolved_mentions == ()
    assert diagnose_draft(response_row, draft) == (
        "people",
        "attributes",
        "time_basis",
        "time_precision",
        "unresolved_mentions",
    )


def diagnose_draft(row: Mapping[str, object], draft: StatementDraft) -> tuple[str, ...]:
    """Name structured response fields that the current parser did not retain."""
    fields = (
        "people",
        "attributes",
        "time_basis",
        "time_precision",
        "unresolved_mentions",
    )
    return tuple(
        field
        for field in fields
        if row.get(field) not in (None, (), [], "")
        and row.get(field) != getattr(draft, field)
    )


def test_capture_harness_still_refuses_unknown_audience(tmp_path) -> None:
    harness = CaptureHarness(tmp_path, registry=Registry({}))
    try:
        harness.activate()
        harness.observe("Synthetic tuning source.")
        harness.advance(60_001)

        report = harness.build_worker().run_due(now_ms=harness.now)

        assert report.refusals.get("unknown_audience") == 1
        assert harness.statements() == []
    finally:
        harness.close()


def test_eligible_people_offer_only_the_source_author(tmp_path, monkeypatch) -> None:
    harness = CaptureHarness(tmp_path)
    try:
        people = {AUTHOR: "person-author", OTHER: "person-subject"}
        monkeypatch.setattr(
            harness.knowledge,
            "person_for_principal",
            lambda principal: people.get(principal),
        )
        source = ObservedEvent(
            event_id="author-source",
            revision=1,
            channel="whatsapp",
            chat_id="synthetic@g.us",
            principal=AUTHOR,
            occurred_ms=1,
            created_ms=1,
            text="Synthetic tuning source.",
        )
        subject = ObservedEvent(
            event_id="subject-source",
            revision=1,
            channel="whatsapp",
            chat_id="synthetic@g.us",
            principal=OTHER,
            occurred_ms=2,
            created_ms=2,
            text="Synthetic subject source.",
        )

        eligible = harness.build_worker()._eligible_people((source, subject), source)

        assert eligible == ("person-author",)
        assert "person-subject" not in eligible
    finally:
        harness.close()
