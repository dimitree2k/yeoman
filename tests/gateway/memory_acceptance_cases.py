"""Synthetic, source-shaped cases and validation for offline memory acceptance work.

No runtime originals, native identifiers, or owner labels belong in this module.
Those stay in the private preservation artifacts and are never imported by tests.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

SCHEMA_VERSION = 1
RECALL_CATEGORIES = (
    "single_segment",
    "multi_segment",
    "temporal",
    "update",
    "abstention",
    "audience",
)
LABEL_AXES = ("mode", "kind", "durability", "importance", "sensitivity")
_PARTITIONS = ("tuning", "held_out")
_AUDIENCE_STATUSES = ("known", "unknown", "author_only")


def _source(
    event_id: str,
    *,
    provenance: str = "synthetic",
    audience_status: str = "known",
    occurred_at_ms: int | None = 1_800_000_000_000,
    time_certainty: str = "synthetic",
    derived_from_event_id: str | None = None,
) -> dict[str, Any]:
    return {
        "source_ref_id": f"ref:{event_id}",
        "event_id": event_id,
        "revision": 1,
        "locator": f"synthetic://{event_id}",
        "sha256": hashlib.sha256(f"synthetic:{event_id}".encode()).hexdigest(),
        "provenance": provenance,
        "audience_status": audience_status,
        "occurred_at_ms": occurred_at_ms,
        "time_certainty": time_certainty,
        "derived_from_event_id": derived_from_event_id,
    }


def _axes(
    *,
    mode: Sequence[str] = ("reported",),
    kind: Sequence[str] = ("claim",),
    durability: Sequence[str] = ("episodic",),
    importance: Sequence[str] = ("ordinary",),
    sensitivity: Sequence[str] = ("ordinary",),
) -> dict[str, dict[str, Any]]:
    labels = {
        "mode": mode,
        "kind": kind,
        "durability": durability,
        "importance": importance,
        "sensitivity": sensitivity,
    }
    return {
        axis: {"status": "labeled", "labels": list(values), "score": 0.9}
        for axis, values in labels.items()
    }


def _case(
    case_id: str,
    category: str,
    partition: str,
    source_refs: Sequence[Mapping[str, Any]],
    *,
    mode: Sequence[str] = ("reported",),
    kind: Sequence[str] = ("claim",),
    durability: Sequence[str] = ("episodic",),
    importance: Sequence[str] = ("ordinary",),
    sensitivity: Sequence[str] = ("ordinary",),
    expected_subjects: Sequence[Mapping[str, Any]] = (),
    expected_claims: Sequence[Mapping[str, Any]] = (),
    expected_answer: str | None = "A synthetic answer.",
    abstention_reason: str | None = None,
    expected_disclosure: str = "group",
    segment_boundaries: Sequence[Mapping[str, str]] = (),
    context_only_refs: Sequence[str] = (),
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "case_id": case_id,
        "trace_status": "synthetic",
        "partition": partition,
        "conversation_group_id": f"conversation:{case_id}",
        "source_group_id": f"source-group:{case_id}",
        "duplicate_group_id": f"duplicate-group:{case_id}",
        "category": category,
        "source_refs": [dict(source) for source in source_refs],
        "transport_speaker": {
            "status": "synthetic",
            "attribution_basis": "synthetic_transport_fixture",
        },
        "expected_subjects": [dict(subject) for subject in expected_subjects],
        "expected_claims": [dict(claim) for claim in expected_claims],
        "expected_answer": expected_answer,
        "abstention_reason": abstention_reason,
        "expected_disclosure": expected_disclosure,
        "labels": _axes(
            mode=mode,
            kind=kind,
            durability=durability,
            importance=importance,
            sensitivity=sensitivity,
        ),
        "segment_boundaries": [dict(boundary) for boundary in segment_boundaries],
        "context_only_refs": list(context_only_refs),
    }


SYNTHETIC_ACCEPTANCE_CASES: tuple[dict[str, Any], ...] = (
    _case(
        "synthetic-reported-claim",
        "single_segment",
        "tuning",
        [_source("event:reported-claim")],
        expected_subjects=[
            {
                "subject_ref": "subject:sample-a",
                "status": "expected",
                "attribution_basis": "explicit_synthetic_name",
            }
        ],
        expected_claims=[
            {
                "claim_id": "claim:reported-parcel",
                "status": "expected",
                "source_ref_ids": ["ref:event:reported-claim"],
            }
        ],
        expected_answer="A synthetic speaker reported that sample subject A received a parcel.",
    ),
    _case(
        "synthetic-corrected-residence",
        "update",
        "held_out",
        [_source("event:old-residence"), _source("event:corrected-residence")],
        expected_claims=[
            {
                "claim_id": "claim:corrected-residence",
                "status": "expected",
                "source_ref_ids": ["ref:event:corrected-residence"],
            }
        ],
        expected_answer="The later synthetic residence statement supersedes the earlier one.",
        segment_boundaries=[
            {
                "start_ref": "ref:event:old-residence",
                "end_ref": "ref:event:corrected-residence",
                "label": "state_update",
            }
        ],
    ),
    _case(
        "synthetic-ambiguous-names",
        "abstention",
        "held_out",
        [_source("event:ambiguous-name")],
        expected_subjects=[
            {
                "subject_ref": None,
                "status": "unresolved",
                "attribution_basis": "ambiguous_synthetic_name",
            }
        ],
        expected_claims=[],
        expected_answer=None,
        abstention_reason="The synthetic name matches more than one possible subject.",
        expected_disclosure="unresolved",
    ),
    _case(
        "synthetic-pronoun-reference",
        "multi_segment",
        "tuning",
        [_source("event:pronoun-antecedent"), _source("event:pronoun-claim")],
        expected_subjects=[
            {
                "subject_ref": "subject:sample-b",
                "status": "expected",
                "attribution_basis": "synthetic_pronoun_antecedent",
            }
        ],
        expected_claims=[
            {
                "claim_id": "claim:pronoun-choice",
                "status": "expected",
                "source_ref_ids": ["ref:event:pronoun-claim"],
            }
        ],
        expected_answer="The pronoun refers to synthetic sample subject B.",
        segment_boundaries=[
            {
                "start_ref": "ref:event:pronoun-antecedent",
                "end_ref": "ref:event:pronoun-claim",
                "label": "cross_message_reference",
            }
        ],
        context_only_refs=["ref:event:pronoun-antecedent"],
    ),
    _case(
        "synthetic-joke-literal-mixture",
        "single_segment",
        "tuning",
        [_source("event:joke-literal")],
        mode=("joke", "literal"),
        expected_claims=[
            {
                "claim_id": "claim:joke-and-literal",
                "status": "expected",
                "source_ref_ids": ["ref:event:joke-literal"],
            }
        ],
        expected_answer="The synthetic message mixes a joke with a literal statement.",
    ),
    _case(
        "synthetic-news-opinion-mixture",
        "single_segment",
        "held_out",
        [_source("event:news-opinion")],
        mode=("news", "opinion"),
        expected_claims=[
            {
                "claim_id": "claim:news-and-opinion",
                "status": "expected",
                "source_ref_ids": ["ref:event:news-opinion"],
            }
        ],
        expected_answer="Separate the reported synthetic news from the speaker's opinion.",
    ),
    _case(
        "synthetic-cross-message-question-answer",
        "multi_segment",
        "tuning",
        [
            _source("event:question-context"),
            _source("event:question"),
            _source("event:answer"),
        ],
        expected_claims=[
            {
                "claim_id": "claim:cross-message-answer",
                "status": "expected",
                "source_ref_ids": ["ref:event:answer"],
            }
        ],
        expected_answer="The answer is the synthetic value stated in the follow-up message.",
        segment_boundaries=[
            {
                "start_ref": "ref:event:question",
                "end_ref": "ref:event:answer",
                "label": "question_answer_pair",
            }
        ],
        context_only_refs=["ref:event:question-context"],
    ),
    _case(
        "synthetic-media-derived-claim",
        "single_segment",
        "held_out",
        [
            _source("event:sample-image", provenance="native_image"),
            _source(
                "event:sample-description",
                provenance="derived_description",
                derived_from_event_id="event:sample-image",
            ),
        ],
        expected_claims=[
            {
                "claim_id": "claim:media-description-only",
                "status": "expected",
                "source_ref_ids": ["ref:event:sample-description"],
            }
        ],
        expected_answer="Use only the synthetic description linked to the preserved image source.",
        context_only_refs=["ref:event:sample-image"],
    ),
    _case(
        "synthetic-unknown-audience",
        "audience",
        "held_out",
        [_source("event:unknown-audience", audience_status="unknown")],
        expected_claims=[],
        expected_answer=None,
        abstention_reason="Unknown source audience cannot establish a group disclosure scope.",
        expected_disclosure="unresolved",
        sensitivity=("personal",),
    ),
    _case(
        "synthetic-temporal-change",
        "temporal",
        "tuning",
        [
            _source("event:prior-time", occurred_at_ms=1_800_000_000_000),
            _source("event:current-time", occurred_at_ms=1_800_086_400_000),
        ],
        expected_claims=[
            {
                "claim_id": "claim:time-bounded-state",
                "status": "expected",
                "source_ref_ids": ["ref:event:prior-time", "ref:event:current-time"],
            }
        ],
        expected_answer="The synthetic state differs between the two explicit time points.",
        segment_boundaries=[
            {
                "start_ref": "ref:event:prior-time",
                "end_ref": "ref:event:current-time",
                "label": "temporal_change",
            }
        ],
    ),
)


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_labels(case: Mapping[str, Any], location: str) -> tuple[int, int]:
    labels = case.get("labels")
    if not isinstance(labels, Mapping) or set(labels) != set(LABEL_AXES):
        raise ValueError(f"{location}: every classification axis must be present exactly once")
    pending = 0
    labeled = 0
    for axis in LABEL_AXES:
        label = labels[axis]
        if not isinstance(label, Mapping):
            raise ValueError(f"{location}: {axis} label must be a mapping")
        status = label.get("status")
        values = label.get("labels")
        score = label.get("score")
        if not isinstance(values, list) or any(not _text(value) for value in values):
            raise ValueError(f"{location}: {axis} labels must be a list of nonempty strings")
        if status == "pending":
            if values or score is not None:
                raise ValueError(f"{location}: pending {axis} labels must remain unknown, not zero")
            pending += 1
        elif status == "labeled":
            if not values or isinstance(score, bool) or not isinstance(score, (int, float)):
                raise ValueError(f"{location}: labeled {axis} requires labels and a score")
            if not math.isfinite(float(score)) or not 0 <= score <= 1:
                raise ValueError(f"{location}: {axis} score must be independent and in [0, 1]")
            if len(values) != len(set(values)):
                raise ValueError(f"{location}: {axis} labels must be unique")
            labeled += 1
        else:
            raise ValueError(f"{location}: {axis} status must be labeled or pending")
    return labeled, pending


def validate_acceptance_cases(cases: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """Validate schema, source linkage, labels, and leakage-safe frozen partitions."""
    if isinstance(cases, (str, bytes)) or not isinstance(cases, Sequence):
        raise ValueError("cases must be a sequence of mappings")

    ids: set[str] = set()
    source_partitions: dict[tuple[str, int], str] = {}
    event_partitions: dict[str, str] = {}
    group_partitions: dict[tuple[str, str], str] = {}
    categories: Counter[str] = Counter()
    provenances: Counter[str] = Counter()
    revisions: set[tuple[str, int]] = set()
    source_groups: set[str] = set()
    conversation_groups: set[str] = set()
    duplicate_groups: set[str] = set()
    counts = Counter(
        cases=0,
        tuning_cases=0,
        held_out_cases=0,
        unresolved_cases=0,
        labeled_label_axes=0,
        pending_label_axes=0,
        unknown_audience_cases=0,
        source_revisions=0,
        source_groups=0,
        conversation_groups=0,
        duplicate_groups=0,
        segment_boundaries=0,
        context_only_refs=0,
    )

    for index, case in enumerate(cases):
        location = f"cases[{index}]"
        if not isinstance(case, Mapping):
            raise ValueError(f"{location}: case must be a mapping")
        if case.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"{location}: schema_version must be {SCHEMA_VERSION}")
        case_id = case.get("case_id")
        if not _text(case_id) or case_id in ids:
            raise ValueError(f"{location}: case_id must be present and unique")
        ids.add(case_id)

        trace_status = case.get("trace_status")
        if trace_status not in {"synthetic", "resolved", "unresolved"}:
            raise ValueError(f"{location}: trace_status is invalid")
        partition = case.get("partition")
        if trace_status == "unresolved":
            if partition is not None:
                raise ValueError(f"{location}: unresolved traces cannot enter a split")
            counts["unresolved_cases"] += 1
        elif partition not in _PARTITIONS:
            raise ValueError(f"{location}: assigned cases require tuning or held_out")
        else:
            counts[f"{partition}_cases"] += 1

        category = case.get("category")
        if trace_status == "unresolved" and category is None:
            categories["unclassified_unresolved"] += 1
        elif category not in RECALL_CATEGORIES:
            raise ValueError(f"{location}: category is invalid")
        else:
            categories[category] += 1

        for field, target in (
            ("conversation_group_id", conversation_groups),
            ("source_group_id", source_groups),
            ("duplicate_group_id", duplicate_groups),
        ):
            group_id = case.get(field)
            if group_id is None and trace_status == "unresolved":
                continue
            if not _text(group_id):
                raise ValueError(f"{location}: {field} is required for a split case")
            target.add(group_id)
            if partition in _PARTITIONS:
                previous = group_partitions.setdefault((field, group_id), partition)
                if previous != partition:
                    raise ValueError(f"{location}: {field} crosses tuning and held_out")

        source_refs = case.get("source_refs")
        if not isinstance(source_refs, list):
            raise ValueError(f"{location}: source_refs must be a list")
        if not source_refs and trace_status != "unresolved":
            raise ValueError(f"{location}: assigned cases require at least one source")
        ref_ids: set[str] = set()
        event_ids: set[str] = set()
        derived_parents: list[str] = []
        has_unknown_audience = False
        claim_ref_ids: set[str] = set()
        for source_index, source in enumerate(source_refs):
            source_location = f"{location}.source_refs[{source_index}]"
            if not isinstance(source, Mapping):
                raise ValueError(f"{source_location}: source ref must be a mapping")
            source_ref_id = source.get("source_ref_id")
            if not _text(source_ref_id) or source_ref_id in ref_ids:
                raise ValueError(f"{source_location}: source_ref_id must be present and unique")
            ref_ids.add(source_ref_id)
            event_id = source.get("event_id")
            revision = source.get("revision")
            if event_id is None or revision is None:
                if trace_status != "unresolved" or event_id is not None or revision is not None:
                    raise ValueError(f"{source_location}: event_id and revision must be known together")
            else:
                if not _text(event_id) or isinstance(revision, bool) or not isinstance(revision, int):
                    raise ValueError(f"{source_location}: event_id and revision are invalid")
                if revision < 1:
                    raise ValueError(f"{source_location}: revision must be positive")
                event_ids.add(event_id)
                key = (event_id, revision)
                revisions.add(key)
                if partition in _PARTITIONS:
                    previous = source_partitions.setdefault(key, partition)
                    if previous != partition:
                        raise ValueError(f"{location}: source event revision crosses partitions")
                    previous_event = event_partitions.setdefault(event_id, partition)
                    if previous_event != partition:
                        raise ValueError(f"{location}: source event crosses tuning and held_out")

            locator = source.get("locator")
            if not (_text(locator) or isinstance(locator, Mapping)):
                raise ValueError(f"{source_location}: locator is required")
            digest = source.get("sha256")
            if not _text(digest) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
                raise ValueError(f"{source_location}: sha256 must be a 64-character hex digest")
            provenance = source.get("provenance")
            if not _text(provenance):
                raise ValueError(f"{source_location}: provenance is required")
            provenances[provenance] += 1
            audience = source.get("audience_status")
            if audience not in _AUDIENCE_STATUSES:
                raise ValueError(f"{source_location}: audience_status is invalid")
            if audience == "unknown":
                has_unknown_audience = True
            occurred = source.get("occurred_at_ms")
            if occurred is not None and (isinstance(occurred, bool) or not isinstance(occurred, int)):
                raise ValueError(f"{source_location}: occurred_at_ms must be an integer or unknown")
            if not _text(source.get("time_certainty")):
                raise ValueError(f"{source_location}: time_certainty must be explicit")
            derived_from = source.get("derived_from_event_id")
            if derived_from is not None:
                if not _text(derived_from):
                    raise ValueError(f"{source_location}: derived_from_event_id is invalid")
                derived_parents.append(derived_from)
        if any(parent not in event_ids for parent in derived_parents):
            raise ValueError(f"{location}: derived media source must link to a source event")
        counts["unknown_audience_cases"] += int(has_unknown_audience)

        speaker = case.get("transport_speaker")
        if not isinstance(speaker, Mapping) or speaker.get("status") not in {
            "synthetic", "verified", "unverified", "unknown"
        } or not _text(speaker.get("attribution_basis")):
            raise ValueError(f"{location}: transport speaker and attribution basis are required")

        subjects = case.get("expected_subjects")
        if not isinstance(subjects, list):
            raise ValueError(f"{location}: expected_subjects must be a list")
        for subject in subjects:
            if not isinstance(subject, Mapping) or subject.get("status") not in {
                "expected", "unresolved", "unknown"
            } or not _text(subject.get("attribution_basis")):
                raise ValueError(f"{location}: expected subject attribution is incomplete")
            subject_ref = subject.get("subject_ref")
            if subject_ref is not None and not _text(subject_ref):
                raise ValueError(f"{location}: subject_ref is invalid")

        claims = case.get("expected_claims")
        if not isinstance(claims, list):
            raise ValueError(f"{location}: expected_claims must be a list")
        for claim in claims:
            if not isinstance(claim, Mapping) or not _text(claim.get("claim_id")):
                raise ValueError(f"{location}: expected claim id is required")
            if claim.get("status") not in {"expected", "unresolved"}:
                raise ValueError(f"{location}: expected claim status is invalid")
            claim_sources = claim.get("source_ref_ids")
            if not isinstance(claim_sources, list) or any(
                source_ref not in ref_ids for source_ref in claim_sources
            ):
                raise ValueError(f"{location}: expected claim source refs must be present")
            claim_ref_ids.update(claim_sources)

        answer = case.get("expected_answer")
        abstention = case.get("abstention_reason")
        if answer is None:
            if not _text(abstention):
                raise ValueError(f"{location}: abstention reason is required when answer is unknown")
        elif not isinstance(answer, str) or abstention is not None:
            raise ValueError(f"{location}: answer and abstention reason conflict")
        disclosure = case.get("expected_disclosure")
        if disclosure not in {"group", "author_only", "unresolved"}:
            raise ValueError(f"{location}: expected_disclosure is invalid")
        if disclosure == "group" and (
            not source_refs or any(source.get("audience_status") != "known" for source in source_refs)
        ):
            raise ValueError(f"{location}: unknown source audience cannot label group disclosure")

        context_only = case.get("context_only_refs")
        if not isinstance(context_only, list) or any(
            not _text(source_ref) or source_ref not in ref_ids for source_ref in context_only
        ):
            raise ValueError(f"{location}: context-only refs must identify source refs")
        if set(context_only) & claim_ref_ids:
            raise ValueError(f"{location}: context-only refs cannot support expected claims")
        counts["context_only_refs"] += len(context_only)

        boundaries = case.get("segment_boundaries")
        if not isinstance(boundaries, list):
            raise ValueError(f"{location}: segment_boundaries must be a list")
        for boundary in boundaries:
            if (
                not isinstance(boundary, Mapping)
                or boundary.get("start_ref") not in ref_ids
                or boundary.get("end_ref") not in ref_ids
                or not _text(boundary.get("label"))
            ):
                raise ValueError(f"{location}: segment boundary refs and label are required")
        counts["segment_boundaries"] += len(boundaries)

        labeled, pending = _validate_labels(case, location)
        counts["labeled_label_axes"] += labeled
        counts["pending_label_axes"] += pending
        counts["cases"] += 1

    counts["source_revisions"] = len(revisions)
    counts["source_groups"] = len(source_groups)
    counts["conversation_groups"] = len(conversation_groups)
    counts["duplicate_groups"] = len(duplicate_groups)
    for category in RECALL_CATEGORIES:
        counts[f"category.{category}"] = categories[category]
    counts["category.unclassified_unresolved"] = categories["unclassified_unresolved"]
    for provenance, count in provenances.items():
        counts[f"provenance.{provenance}"] = count
    return dict(counts)
