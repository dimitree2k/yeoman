"""As-of Knowledge recall: nothing created, corrected or sourced after t is visible.

Synthetic temporary stores only; the source ledger is populated through the public
capture path, never by editing runtime data.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from yeoman_gateway.knowledge.models import RecallQuery, ValidationError

from tests.gateway.knowledge.conftest import ProjectedKnowledgeHarness

T0 = 1_700_000_000_000
MINUTE = 60_000


def _statement(
    h: ProjectedKnowledgeHarness, text: str, *, when_ms: int, tom: str, members: set[str]
):
    source = h.source(tom, audience=members, when_ms=when_ms)
    # Capture at the source instant, so ``created_ms`` itself is part of the as-of test.
    h.clock.value_ms = int(when_ms)
    return h.capture_text(text, source).statement_ids[0]


def test_recall_as_of_excludes_created_or_corrected_after_t(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import yeoman_gateway.knowledge._statements as statements_module

    h = ProjectedKnowledgeHarness(tmp_path)
    monkeypatch.setattr(statements_module, "now_ms", lambda: h.clock.now_ms())
    try:
        tom, alex = h.two_people()
        members = {h.principal_for(tom), h.principal_for(alex)}
        h.clock.value_ms = T0
        early = _statement(h, "early plan is visible", when_ms=T0, tom=tom, members=members)
        later = _statement(
            h,
            "later plan is invisible at t",
            when_ms=T0 + 10 * MINUTE,
            tom=tom,
            members=members,
        )

        context = h.read_context(tom, recipients=members)
        at_t0 = h.service.recall(RecallQuery(text="plan", limit=10, as_of_ms=T0), context=context)
        now = h.service.recall(RecallQuery(text="plan", limit=10), context=context)

        assert early in at_t0.statement_ids
        assert later not in at_t0.statement_ids
        assert early in now.statement_ids and later in now.statement_ids

        # Correcting the early statement after t makes its old form invisible at t too.
        original_source = h.source(tom, audience=members, when_ms=T0)
        original = h.capture_text(
            "original corrected fact", original_source, subjects=(alex,)
        ).statement_ids[0]
        h.clock.value_ms = T0 + 20 * MINUTE
        replacement = h.candidate("replacement fact after t", original_source, subjects=(alex,))
        h.service.correct_statement(
            original,
            replacement,
            expected_source=original_source,
            context=h.capture_context(original_source),
        )
        corrected = h.service.recall(
            RecallQuery(text="fact", limit=10, as_of_ms=T0), context=context
        )
        assert original not in corrected.statement_ids
        assert "replacement fact after t" not in corrected.text
    finally:
        h.close()


def test_recall_as_of_none_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import yeoman_gateway.knowledge._statements as statements_module

    h = ProjectedKnowledgeHarness(tmp_path)
    monkeypatch.setattr(statements_module, "now_ms", lambda: h.clock.now_ms())
    try:
        tom, alex = h.two_people()
        members = {h.principal_for(tom), h.principal_for(alex)}
        h.clock.value_ms = T0
        _statement(h, "one stable fact", when_ms=T0, tom=tom, members=members)
        context = h.read_context(tom, recipients=members)

        implicit = h.service.recall(RecallQuery(text="fact", limit=10), context=context)
        explicit_none = h.service.recall(
            RecallQuery(text="fact", limit=10, as_of_ms=None), context=context
        )

        assert implicit.statement_ids == explicit_none.statement_ids
        assert implicit.text == explicit_none.text
        assert explicit_none.reason == "ok"
    finally:
        h.close()


def test_recall_as_of_rejects_negative_instant() -> None:
    with pytest.raises(ValidationError):
        RecallQuery(text="x", as_of_ms=-1)
