"""Estimate offline extraction costs from prebuilt segment rows.

`--segments` is a list of requests with a `targets` list, separate `context_tokens`,
`expected_output_tokens`, and `output_cap_tokens`. Each target has `event_id`,
`revision`, `tokens`, `source_class`, and `audience_scope`. `--rates` contains numeric
per-million input/output rates and optional separate evidence metadata; missing rates
stay unknown. This module performs no model or network calls.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

_PRICE_FIELDS = {"input_per_million_tokens", "output_per_million_tokens"}
_AUDIENCE_SCOPES = {"eligible", "excluded", "unknown"}
_RATE_UNIT = "currency units per 1,000,000 tokens"


def estimate_rebuild(
    segments: Sequence[Mapping[str, Any]],
    rates: Mapping[str, Mapping[str, float]],
) -> dict[str, Any]:
    """Estimate cost without provider calls; target revisions count once, request context per row."""
    if isinstance(segments, (str, bytes)) or not isinstance(segments, Sequence):
        raise ValueError("segments must be a sequence of segment mappings")
    if not isinstance(rates, Mapping):
        raise ValueError("rates must be a model-to-rates mapping")

    target_tokens: dict[tuple[str, str | int], int] = {}
    target_source_classes: dict[tuple[str, str | int], set[str]] = defaultdict(set)
    target_audience_scopes: dict[tuple[str, str | int], set[str]] = defaultdict(set)
    source_copy_classes: Counter[str] = Counter()
    source_copy_scopes: Counter[str] = Counter()
    context_tokens = 0
    expected_output_tokens = 0
    output_cap_tokens = 0
    expected_known = True
    cap_known = True
    source_copy_count = 0

    for index, segment in enumerate(segments):
        if not isinstance(segment, Mapping):
            raise ValueError(f"segment {index} must be a mapping")
        targets = segment.get("targets")
        if isinstance(targets, (str, bytes)) or not isinstance(targets, Sequence):
            raise ValueError(f"segment {index} targets must be a sequence")
        context_tokens += _token_count(segment.get("context_tokens"), f"segment {index} context_tokens")
        expected = _optional_token_count(segment.get("expected_output_tokens"), f"segment {index} expected_output_tokens")
        cap = _optional_token_count(segment.get("output_cap_tokens"), f"segment {index} output_cap_tokens")
        if expected is None:
            expected_known = False
        else:
            expected_output_tokens += expected
        if cap is None:
            cap_known = False
        else:
            output_cap_tokens += cap

        for target_index, target in enumerate(targets):
            if not isinstance(target, Mapping):
                raise ValueError(f"segment {index} target {target_index} must be a mapping")
            event_id = target.get("event_id")
            revision = target.get("revision")
            if not isinstance(event_id, str) or not event_id.strip():
                raise ValueError(f"segment {index} target {target_index} needs an event_id")
            if isinstance(revision, bool) or not isinstance(revision, (str, int)) or revision == "":
                raise ValueError(f"segment {index} target {target_index} needs a string or integer revision")
            key = (event_id, revision)
            tokens = _token_count(target.get("tokens"), f"segment {index} target {target_index} tokens")
            prior_tokens = target_tokens.setdefault(key, tokens)
            if prior_tokens != tokens:
                raise ValueError("duplicate event revision has inconsistent target token counts")
            source_class = target.get("source_class", "unknown")
            if not isinstance(source_class, str) or not source_class.strip():
                raise ValueError(f"segment {index} target {target_index} source_class must be non-empty")
            audience_scope = target.get("audience_scope", "unknown")
            if not isinstance(audience_scope, str) or audience_scope not in _AUDIENCE_SCOPES:
                raise ValueError(f"segment {index} target {target_index} has invalid audience_scope")
            target_source_classes[key].add(source_class)
            target_audience_scopes[key].add(audience_scope)
            source_copy_classes[source_class] += 1
            source_copy_scopes[audience_scope] += 1
            source_copy_count += 1

    unique_target_tokens = sum(target_tokens.values())
    total_input_tokens = unique_target_tokens + context_tokens
    target_classes = Counter(_single_or_conflict(classes) for classes in target_source_classes.values())
    audience_targets = Counter(_single_or_conflict(scopes) for scopes in target_audience_scopes.values())
    tokens = {
        "unique_target_tokens": unique_target_tokens,
        "context_tokens": context_tokens,
        "input_tokens": total_input_tokens,
        "expected_output_tokens": expected_output_tokens if expected_known else None,
        "output_cap_tokens": output_cap_tokens if cap_known else None,
    }
    model_results: dict[str, Any] = {}
    for model, model_rates in rates.items():
        if not isinstance(model, str) or not model.strip() or not isinstance(model_rates, Mapping):
            raise ValueError("each rate entry needs a non-empty model name and rate mapping")
        unknown_rate_fields = set(model_rates) - _PRICE_FIELDS
        if unknown_rate_fields:
            raise ValueError(f"rate entry for {model!r} has unsupported fields")
        input_rate = _rate(model_rates.get("input_per_million_tokens"), model, "input")
        output_rate = _rate(model_rates.get("output_per_million_tokens"), model, "output")
        expected_tokens = tokens["expected_output_tokens"]
        cap_tokens = tokens["output_cap_tokens"]
        expected_input_cost = _cost(total_input_tokens, input_rate)
        cap_input_cost = expected_input_cost
        expected_output_cost = _cost(expected_tokens, output_rate)
        cap_output_cost = _cost(cap_tokens, output_rate)
        model_results[model] = {
            "currency": None,
            "price_date": None,
            "source_url": None,
            "accessed_date": None,
            "price_basis": None,
            "rate_unit": _RATE_UNIT,
            "input_per_million_tokens": input_rate,
            "output_per_million_tokens": output_rate,
            "expected": {
                "input_cost": expected_input_cost,
                "output_cost": expected_output_cost,
                "total_cost": _total(expected_input_cost, expected_output_cost),
            },
            "output_cap": {
                "input_cost": cap_input_cost,
                "output_cost": cap_output_cost,
                "total_cost": _total(cap_input_cost, cap_output_cost),
            },
            "unknowns": _unknowns(input_rate, output_rate, expected_tokens, cap_tokens),
        }

    return {
        "schema_version": 1,
        "coverage": {
            "request_count": len(segments),
            "source_copy_count": source_copy_count,
            "unique_target_revisions": len(target_tokens),
            "source_copies_by_source_class": dict(sorted(source_copy_classes.items())),
            "target_revisions_by_source_class": dict(sorted(target_classes.items())),
            "source_copies_by_audience_scope": dict(sorted(source_copy_scopes.items())),
            "target_revisions_by_audience_scope": dict(sorted(audience_targets.items())),
            "eligible_target_revisions": audience_targets["eligible"],
        },
        "tokens": tokens,
        "models": model_results,
    }


def _token_count(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _optional_token_count(value: Any, label: str) -> int | None:
    return None if value is None else _token_count(value, label)


def _rate(value: Any, model: str, direction: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{direction} rate for {model!r} must be finite and non-negative")
    rate = float(value)
    if not math.isfinite(rate) or rate < 0:
        raise ValueError(f"{direction} rate for {model!r} must be finite and non-negative")
    return rate


def _cost(tokens: int | None, rate: float | None) -> float | None:
    return None if tokens is None or rate is None else tokens * rate / 1_000_000


def _total(first: float | None, second: float | None) -> float | None:
    return None if first is None or second is None else first + second


def _single_or_conflict(values: set[str]) -> str:
    return next(iter(values)) if len(values) == 1 else "conflict"


def _unknowns(
    input_rate: float | None,
    output_rate: float | None,
    expected_tokens: int | None,
    cap_tokens: int | None,
) -> list[str]:
    unknowns = []
    if input_rate is None:
        unknowns.append("input rate unavailable")
    if output_rate is None:
        unknowns.append("output rate unavailable")
    if expected_tokens is None:
        unknowns.append("expected output token count unavailable")
    if cap_tokens is None:
        unknowns.append("output cap token count unavailable")
    return unknowns


def _read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--segments", required=True, type=Path)
    parser.add_argument("--rates", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)

    segment_rows = _read_json(args.segments)
    rate_document = _read_json(args.rates)
    if not isinstance(rate_document, Mapping) or not isinstance(rate_document.get("rates"), Mapping):
        parser.error("--rates must contain a 'rates' object and optional 'evidence' object")
    result = estimate_rebuild(segment_rows, rate_document["rates"])
    evidence = rate_document.get("evidence", {})
    if not isinstance(evidence, Mapping):
        parser.error("--rates evidence must be an object")
    for model, metadata in evidence.items():
        if model not in result["models"] or not isinstance(metadata, Mapping):
            parser.error("--rates evidence must map estimated model names to objects")
        for field in ("currency", "price_date", "source_url", "accessed_date", "price_basis"):
            value = metadata.get(field)
            if value is not None and not isinstance(value, str):
                parser.error(f"--rates evidence {field} values must be strings or null")
            result["models"][model][field] = value
    with args.output.open("w", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)
        stream.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
