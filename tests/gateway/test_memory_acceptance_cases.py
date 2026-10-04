"""Source-grounded acceptance-set validation."""

from __future__ import annotations

from copy import deepcopy

import pytest

from tests.gateway.memory_acceptance_cases import (
    LABEL_AXES,
    RECALL_CATEGORIES,
    SYNTHETIC_ACCEPTANCE_CASES,
    validate_acceptance_cases,
)


def _case(case_id: str) -> dict[str, object]:
    return deepcopy(next(case for case in SYNTHETIC_ACCEPTANCE_CASES if case["case_id"] == case_id))


def test_synthetic_acceptance_manifest_covers_all_recall_categories() -> None:
    coverage = validate_acceptance_cases(SYNTHETIC_ACCEPTANCE_CASES)

    assert all(coverage[f"category.{category}"] > 0 for category in RECALL_CATEGORIES)
    assert coverage["cases"] == 10
    assert coverage["tuning_cases"] + coverage["held_out_cases"] == 10


def test_temporal_fixture_uses_distinct_ordered_source_times() -> None:
    case = _case("synthetic-temporal-change")
    source_times = [source["occurred_at_ms"] for source in case["source_refs"]]

    assert len(source_times) == 2
    assert source_times[0] < source_times[1]


def test_acceptance_split_rejects_shared_source_revision() -> None:
    first = _case("synthetic-reported-claim")
    second = deepcopy(first)
    second["case_id"] = "synthetic-split-leak"
    second["partition"] = "held_out"
    second["conversation_group_id"] = "conversation:independent"
    second["source_group_id"] = "source-group:independent"
    second["duplicate_group_id"] = "duplicate-group:independent"

    with pytest.raises(ValueError, match="source event revision crosses partitions"):
        validate_acceptance_cases([first, second])


def test_acceptance_split_rejects_event_across_revisions() -> None:
    first = _case("synthetic-reported-claim")
    second = deepcopy(first)
    second["case_id"] = "synthetic-split-revision-leak"
    second["partition"] = "held_out"
    second["conversation_group_id"] = "conversation:independent-revision"
    second["source_group_id"] = "source-group:independent-revision"
    second["duplicate_group_id"] = "duplicate-group:independent-revision"
    second["source_refs"][0]["revision"] = 2

    with pytest.raises(ValueError, match="source event crosses tuning and held_out"):
        validate_acceptance_cases([first, second])


def test_unknown_audience_case_never_labels_group_disclosure() -> None:
    case = _case("synthetic-unknown-audience")
    coverage = validate_acceptance_cases([case])

    assert coverage["unknown_audience_cases"] == 1
    assert case["expected_disclosure"] == "unresolved"

    case["expected_disclosure"] = "group"
    with pytest.raises(ValueError, match="unknown source audience"):
        validate_acceptance_cases([case])


def test_context_and_media_sources_keep_their_provenance() -> None:
    cases = deepcopy(SYNTHETIC_ACCEPTANCE_CASES)
    coverage = validate_acceptance_cases(cases)
    media = next(case for case in cases if case["case_id"] == "synthetic-media-derived-claim")
    sources = {source["source_ref_id"]: source for source in media["source_refs"]}
    image = sources["ref:event:sample-image"]
    description = sources["ref:event:sample-description"]
    claim = media["expected_claims"][0]

    assert image["provenance"] == "native_image"
    assert description["provenance"] == "derived_description"
    assert description["derived_from_event_id"] == image["event_id"]
    assert claim["source_ref_ids"] == [description["source_ref_id"]]
    assert image["source_ref_id"] in media["context_only_refs"]
    assert coverage["provenance.native_image"] == 1
    assert coverage["provenance.derived_description"] == 1
    assert coverage["context_only_refs"] > 0

    description["derived_from_event_id"] = "event:missing-image"
    with pytest.raises(ValueError, match="must link to a source event"):
        validate_acceptance_cases(cases)


def test_axes_are_independent_and_unlabeled_is_unknown() -> None:
    case = _case("synthetic-reported-claim")
    labels = case["labels"]
    labels["mode"]["score"] = 0.9
    labels["importance"]["score"] = 0.8
    labels["sensitivity"] = {"status": "pending", "labels": [], "score": None}
    coverage = validate_acceptance_cases([case])

    assert labels["mode"]["score"] + labels["importance"]["score"] > 1
    assert labels["sensitivity"]["status"] == "pending"
    assert labels["sensitivity"]["score"] is None
    assert set(labels) == set(LABEL_AXES)
    assert coverage["pending_label_axes"] == 1
