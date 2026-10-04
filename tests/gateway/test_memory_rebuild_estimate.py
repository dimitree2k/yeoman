"""Offline cost estimates deduplicate target revisions, not request context."""

from __future__ import annotations

import json
import math

import pytest

from scripts.estimate_memory_rebuild import estimate_rebuild, main


def test_estimate_counts_context_tokens_and_deduplicates_targets() -> None:
    segments = [
        {
            "segment_id": "segment-a",
            "targets": [
                {
                    "event_id": "event-1",
                    "revision": 1,
                    "tokens": 100,
                    "source_class": "native",
                    "audience_scope": "eligible",
                }
            ],
            "context_tokens": 300,
            "expected_output_tokens": 10,
            "output_cap_tokens": 50,
        },
        {
            "segment_id": "segment-b",
            "targets": [
                {
                    "event_id": "event-1",
                    "revision": 1,
                    "tokens": 100,
                    "source_class": "native",
                    "audience_scope": "unknown",
                }
            ],
            "context_tokens": 200,
            "expected_output_tokens": 12,
            "output_cap_tokens": 50,
        },
    ]

    result = estimate_rebuild(
        segments,
        {
            "synthetic-model": {
                "input_per_million_tokens": 2.0,
                "output_per_million_tokens": 5.0,
            }
        },
    )

    assert result["coverage"]["request_count"] == 2
    assert result["coverage"]["unique_target_revisions"] == 1
    assert result["tokens"] == {
        "unique_target_tokens": 100,
        "context_tokens": 500,
        "input_tokens": 600,
        "expected_output_tokens": 22,
        "output_cap_tokens": 100,
    }
    estimate = result["models"]["synthetic-model"]
    assert estimate["expected"]["input_cost"] == pytest.approx(0.0012)
    assert estimate["expected"]["output_cost"] == pytest.approx(0.00011)
    assert estimate["expected"]["total_cost"] == pytest.approx(0.00131)
    assert estimate["output_cap"]["total_cost"] == pytest.approx(0.0017)


def test_partial_overlap_deduplicates_revisions_but_keeps_scope_conflicts() -> None:
    segments = [
        {
            "segment_id": "sample-a",
            "targets": [
                {
                    "event_id": "event-1",
                    "revision": 1,
                    "tokens": 100,
                    "source_class": "native",
                    "audience_scope": "eligible",
                },
                {
                    "event_id": "event-1",
                    "revision": 2,
                    "tokens": 120,
                    "source_class": "native",
                    "audience_scope": "excluded",
                },
            ],
            "context_tokens": 10,
            "expected_output_tokens": 3,
            "output_cap_tokens": 8,
        },
        {
            "segment_id": "sample-b",
            "targets": [
                {
                    "event_id": "event-1",
                    "revision": 1,
                    "tokens": 100,
                    "source_class": "derived",
                    "audience_scope": "unknown",
                }
            ],
            "context_tokens": 20,
            "expected_output_tokens": 4,
            "output_cap_tokens": 8,
        },
    ]

    result = estimate_rebuild(segments, {})

    assert result["coverage"] == {
        "request_count": 2,
        "source_copy_count": 3,
        "unique_target_revisions": 2,
        "source_copies_by_source_class": {"derived": 1, "native": 2},
        "target_revisions_by_source_class": {"conflict": 1, "native": 1},
        "source_copies_by_audience_scope": {"eligible": 1, "excluded": 1, "unknown": 1},
        "target_revisions_by_audience_scope": {"conflict": 1, "excluded": 1},
        "eligible_target_revisions": 0,
    }
    assert result["tokens"] == {
        "unique_target_tokens": 220,
        "context_tokens": 30,
        "input_tokens": 250,
        "expected_output_tokens": 7,
        "output_cap_tokens": 16,
    }


def test_unknown_rates_do_not_become_zero_cost() -> None:
    result = estimate_rebuild(
        [
            {
                "segment_id": "segment-a",
                "targets": [],
                "context_tokens": 0,
                "expected_output_tokens": 0,
                "output_cap_tokens": 0,
            }
        ],
        {"unpriced-model": {}},
    )

    model = result["models"]["unpriced-model"]
    assert model["input_per_million_tokens"] is None
    assert model["output_per_million_tokens"] is None
    assert model["expected"]["total_cost"] is None
    assert model["output_cap"]["total_cost"] is None
    assert model["unknowns"][:2] == ["input rate unavailable", "output rate unavailable"]


def test_missing_output_assumptions_remain_unknown() -> None:
    result = estimate_rebuild(
        [{"segment_id": "segment-a", "targets": [], "context_tokens": 10}],
        {"priced-model": {"input_per_million_tokens": 2.0, "output_per_million_tokens": 5.0}},
    )

    model = result["models"]["priced-model"]
    assert result["tokens"]["expected_output_tokens"] is None
    assert result["tokens"]["output_cap_tokens"] is None
    assert model["expected"]["input_cost"] == pytest.approx(0.00002)
    assert model["expected"]["output_cost"] is None
    assert model["expected"]["total_cost"] is None
    assert model["output_cap"]["total_cost"] is None


def test_inconsistent_copies_of_one_revision_are_rejected() -> None:
    with pytest.raises(ValueError, match="inconsistent target token counts"):
        estimate_rebuild(
            [
                {"targets": [{"event_id": "event-1", "revision": 1, "tokens": 10}], "context_tokens": 0},
                {"targets": [{"event_id": "event-1", "revision": 1, "tokens": 11}], "context_tokens": 0},
            ],
            {},
        )


def test_invalid_audience_scope_type_is_reported_as_bad_input() -> None:
    with pytest.raises(ValueError, match="invalid audience_scope"):
        estimate_rebuild(
            [
                {
                    "targets": [
                        {
                            "event_id": "event-1",
                            "revision": 1,
                            "tokens": 1,
                            "audience_scope": [],
                        }
                    ],
                    "context_tokens": 0,
                }
            ],
            {},
        )


def test_cli_preserves_price_evidence_and_writes_json(tmp_path) -> None:
    segments_path = tmp_path / "segments.json"
    rates_path = tmp_path / "rates.json"
    output_path = tmp_path / "estimate.json"
    segments_path.write_text(
        '[{"segment_id":"segment-a","targets":[],"context_tokens":0,'
        '"expected_output_tokens":0,"output_cap_tokens":0}]',
        encoding="utf-8",
    )
    rates_path.write_text(
        '{"rates":{"candidate":{"input_per_million_tokens":null,'
        '"output_per_million_tokens":null}},"evidence":{"candidate":'
        '{"currency":null,"price_date":null,"source_url":null,'
        '"accessed_date":"2026-10-04","price_basis":"OpenAI API Standard short-context list rate"}}}',
        encoding="utf-8",
    )

    assert main(["--segments", str(segments_path), "--rates", str(rates_path), "--output", str(output_path)]) == 0
    output = json.loads(output_path.read_text(encoding="utf-8"))
    candidate = output["models"]["candidate"]
    assert candidate["currency"] is None
    assert candidate["price_date"] is None
    assert candidate["source_url"] is None
    assert candidate["accessed_date"] == "2026-10-04"
    assert candidate["price_basis"] == "OpenAI API Standard short-context list rate"
    assert candidate["expected"]["total_cost"] is None


@pytest.mark.parametrize("bad_rate", [-1.0, math.inf, math.nan, True])
def test_rates_must_be_finite_and_nonnegative(bad_rate) -> None:
    with pytest.raises(ValueError, match="finite and non-negative"):
        estimate_rebuild(
            [{"targets": [], "context_tokens": 0}],
            {"model": {"input_per_million_tokens": bad_rate}},
        )


@pytest.mark.parametrize("bad_count", [-1, 1.5, True])
def test_token_counts_must_be_nonnegative_integers(bad_count) -> None:
    with pytest.raises(ValueError, match="non-negative integer"):
        estimate_rebuild(
            [{"targets": [], "context_tokens": bad_count}],
            {},
        )
