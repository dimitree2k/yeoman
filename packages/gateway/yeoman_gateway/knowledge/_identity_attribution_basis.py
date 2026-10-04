"""Evidence-bounded, proposal-only historical attribution bases."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from ._identity_audit import _resolve_observed_identifier
from .models import Identifier

_HASH = re.compile(r"^[0-9a-f]{64}$")
_ACCOUNT_SOURCES = ("config", "journal", "bindings")


def validate_attribution_evidence(
    evidence: Mapping[str, Any], *, input_hashes: Mapping[str, str]
) -> dict[str, Any]:
    """Check the bounded v1 shape and bind references to the supplied input pins."""
    if not isinstance(evidence, Mapping) or evidence.get("schema_version") != 1:
        raise ValueError("unsupported attribution evidence")
    hashes = evidence.get("input_hashes")
    if not isinstance(hashes, Mapping) or any(
        hashes.get(name) != digest for name, digest in input_hashes.items()
    ):
        raise ValueError("attribution evidence input hash mismatch")
    if any(
        not isinstance(name, str)
        or not isinstance(digest, str)
        or _HASH.fullmatch(digest) is None
        for name, digest in hashes.items()
    ):
        raise ValueError("malformed attribution evidence input hash")
    period = _period(evidence.get("covered_period"))
    inventories = evidence.get("account_inventories")
    if not isinstance(inventories, list):
        raise ValueError("malformed account inventories")
    for inventory in inventories:
        if not isinstance(inventory, Mapping) or not _text(inventory.get("channel")):
            raise ValueError("malformed account inventory")
        covered = _period(inventory.get("covered_period"))
        if covered[0] < period[0] or covered[1] > period[1]:
            raise ValueError("account inventory period exceeds evidence period")
        sources = inventory.get("sources")
        if not isinstance(sources, Mapping) or set(sources) != set(_ACCOUNT_SOURCES):
            raise ValueError("account inventory sources are incomplete")
        for source_name, source in sources.items():
            if not isinstance(source, Mapping) or not isinstance(source.get("complete"), bool):
                raise ValueError("malformed account inventory completeness")
            if source_name == "config" and source["complete"] and (
                source.get("reviewed") is not True or source.get("coverage_complete") is not True
            ):
                raise ValueError("config inventory lacks reviewed period coverage")
            accounts, refs = source.get("accounts"), source.get("source_refs")
            if not isinstance(accounts, list) or any(not _text(item) for item in accounts):
                raise ValueError("malformed account inventory accounts")
            if not isinstance(refs, list):
                raise ValueError("malformed account inventory references")
            for ref in refs:
                _validate_ref(ref, hashes, covered)
    observations = evidence.get("observations", [])
    spans = evidence.get("continuity_spans", [])
    if not isinstance(observations, list) or not isinstance(spans, list):
        raise ValueError("malformed continuity evidence")
    for observation in observations:
        if not isinstance(observation, Mapping):
            raise ValueError("malformed identifier observation")
        _identifier(observation.get("identifier"))
        at_ms = _timestamp(observation.get("observed_ms"))
        ref = observation.get("source_ref")
        _validate_ref(ref, hashes, period)
        if ref.get("time_ms") != at_ms:
            raise ValueError("observation time does not match its source reference")
        for name in ("name", "provenance_class", "time_certainty"):
            value = observation.get(name)
            if value is not None and not _text(value):
                raise ValueError("malformed observation metadata")
        if observation.get("paired_lid") is not None and _identifier(
            observation["paired_lid"]
        ).kind != "lid":
            raise ValueError("paired identifier is not a LID")
    for span in spans:
        if not isinstance(span, Mapping):
            raise ValueError("malformed phone continuity span")
        _identifier(span.get("identifier"))
        if _identifier(span.get("lid_identifier")).kind != "lid":
            raise ValueError("phone continuity span lacks a LID")
        start, end = _period({"start_ms": span.get("start_ms"), "end_ms": span.get("end_ms")})
        refs = span.get("evidence_refs")
        if not isinstance(refs, list) or not refs:
            raise ValueError("phone continuity span lacks evidence references")
        if span.get("reviewed") is True and span.get("coverage_complete") is True:
            if not all(_text(span.get(key)) for key in ("pair_name", "canonical_person_id", "binding_id")):
                raise ValueError("reviewed phone span lacks reviewed pair identity")
            for ref in refs:
                _validate_ref(ref, hashes, (start, end))
    return dict(evidence)


def account_for_event(
    evidence: Mapping[str, Any], *, channel: str, at_ms: int
) -> tuple[str | None, str | None, list[dict[str, Any]]]:
    """Infer one account only from three complete, agreeing period inventories."""
    at_ms = _timestamp(at_ms)
    covering = [
        item for item in evidence["account_inventories"]
        if item["channel"].casefold() == channel.casefold()
        and _period(item["covered_period"])[0] <= at_ms < _period(item["covered_period"])[1]
    ]
    if len(covering) != 1:
        return None, "account_inventory_incomplete", []
    sources = covering[0]["sources"]
    if any(not sources[name]["complete"] or not sources[name]["source_refs"] for name in _ACCOUNT_SOURCES):
        return None, "account_inventory_incomplete", []
    account_sets = [
        {str(account).casefold(): str(account) for account in sources[name]["accounts"]}
        for name in _ACCOUNT_SOURCES
    ]
    all_accounts = set().union(*(set(accounts) for accounts in account_sets))
    refs = [
        {"source": name, **dict(ref)}
        for name in _ACCOUNT_SOURCES
        for ref in sources[name]["source_refs"]
    ]
    if len(all_accounts) != 1:
        return None, "account_inventory_multiple", refs
    account_key = next(iter(all_accounts))
    if any(set(accounts) != {account_key} for accounts in account_sets):
        return None, "account_inventory_incomplete", refs
    return account_sets[0][account_key], None, refs


def resolve_attribution_binding(
    identifier: Identifier,
    at_ms: int,
    *,
    bindings: Sequence[Mapping[str, Any]],
    canonical_ids: Mapping[str, str],
    observations: Sequence[Mapping[str, Any]],
    continuity_spans: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return strict proof or an evidence-backed candidate; never an authority grant."""
    if not isinstance(identifier, Identifier):
        raise TypeError("identifier must be a typed Identifier")
    at_ms = _timestamp(at_ms)
    rows = [dict(row) for row in bindings]
    strict = _resolve_observed_identifier(
        {"channel": identifier.channel, "kind": identifier.kind,
         "namespace": identifier.namespace, "value": identifier.value},
        {"occurred_ms": at_ms, "time_certainty": "native"},
        rows,
        dict(canonical_ids),
    )
    if strict.get("status") == "resolved":
        binding = next((row for row in rows if row.get("binding_id") == strict.get("binding_id")), None)
        if binding is None:
            return _result("unresolved", reason="binding_missing")
        if _authoritative(binding):
            return _result(
                "resolved", strict["canonical_person_id"], binding.get("binding_id"), "proven",
                [{"kind": "binding", "binding_id": binding.get("binding_id")}],
            )
        return _result("candidate", reason="binding_not_authoritative")
    if strict.get("reason") != "binding_start_unknown":
        return _result("unresolved", reason=strict.get("reason"))

    matching = [row for row in rows if _binding_key(row) == _identifier_key(identifier)]
    anchors = [row for row in matching if _unknown_start(row)]
    if not anchors or any(not _authoritative(row) for row in anchors):
        return _result("unresolved", reason="binding_not_authoritative")
    if any(row.get("status") == "ended" and int(row.get("valid_until_ms") or 0) <= 0 for row in anchors):
        return _result("unresolved", reason="binding_end_unknown")
    anchors = [row for row in anchors if _continuity_binding_covers(row, at_ms)]
    if not anchors:
        return _result("unresolved", reason="outside_proven_period")
    people = {_canonical(row.get("person_id"), canonical_ids) for row in anchors}
    if len(people) != 1:
        return _result("unresolved", reason="binding_start_conflict")
    person = next(iter(people))
    if identifier.kind == "lid":
        refs = _lid_refs(identifier, at_ms, person, observations)
        if refs:
            return _result("candidate", person, None, "observed_continuity", refs)
        return _result("candidate", reason="lid_observation_history_incomplete")
    if identifier.kind == "phone_jid":
        refs, reason = _phone_refs(identifier, at_ms, person, matching, observations, continuity_spans)
        if refs:
            return _result("candidate", person, None, "observed_continuity", refs)
        return _result("candidate", reason=reason)
    return _result("candidate", reason="continuity_not_supported_for_identifier")


def _lid_refs(identifier: Identifier, at_ms: int, person: str,
              observations: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows = [row for row in observations if _same_identifier(row.get("identifier"), identifier)]
    if not rows or any(row.get("canonical_person_id") not in (None, person) or not _valid_observation(row, identifier) for row in rows):
        return []
    times = [int(row["observed_ms"]) for row in rows]
    if not min(times) <= at_ms <= max(times):
        return []
    return [{"kind": "observation", **dict(row["source_ref"])} for row in rows]


def _phone_refs(identifier: Identifier, at_ms: int, person: str,
                bindings: Sequence[Mapping[str, Any]], observations: Sequence[Mapping[str, Any]],
                spans: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    matches = [span for span in spans if _same_identifier(span.get("identifier"), identifier)
               and span.get("canonical_person_id") == person and span.get("reviewed") is True
               and span.get("coverage_complete") is True and _valid_period(span.get("start_ms"), span.get("end_ms"))
               and int(span["start_ms"]) <= at_ms < int(span["end_ms"])
               and any(row.get("binding_id") == span.get("binding_id")
                       and _continuity_binding_covers(row, at_ms)
                       and _authoritative(row) for row in bindings)]
    if len(matches) != 1:
        return [], "phone_continuity_span_missing"
    span = matches[0]
    observed = [row for row in observations if _same_identifier(row.get("identifier"), identifier)
                and int(row.get("observed_ms") or 0) >= span["start_ms"]
                and int(row.get("observed_ms") or 0) < span["end_ms"]]
    if not observed:
        return [], "phone_continuity_observations_missing"
    for row in observed:
        if (row.get("name") != span.get("pair_name")
                or not _same_identifier(row.get("paired_lid"), span.get("lid_identifier"))
                or not _valid_observation(row, identifier)):
            return [], "phone_continuity_pair_break"
    refs = [{"kind": "observation", **dict(row["source_ref"])} for row in observed]
    expected = {_ref_key(row) for row in span["evidence_refs"]}
    if expected != {_ref_key(row["source_ref"]) for row in observed}:
        return [], "phone_continuity_coverage_break"
    return refs, ""


def _valid_observation(row: Mapping[str, Any], identifier: Identifier) -> bool:
    if (not _same_identifier(row.get("identifier"), identifier)
            or row.get("provenance_class") != "native"
            or row.get("time_certainty") not in {"native", "provider_timestamp"}
            or not _timestamp_or_none(row.get("observed_ms"))):
        return False
    ref = row.get("source_ref")
    return isinstance(ref, Mapping) and _text(ref.get("source_id")) and ref.get("time_ms") == row["observed_ms"]


def _continuity_binding_covers(binding: Mapping[str, Any], at_ms: int) -> bool:
    if not _unknown_start(binding) or binding.get("status") not in {"active", "ended"}:
        return False
    valid_until = int(binding.get("valid_until_ms") or 0)
    if binding.get("status") == "ended" and valid_until <= 0:
        return False
    return valid_until <= 0 or at_ms < valid_until


def _unknown_start(binding: Mapping[str, Any]) -> bool:
    return int(binding.get("valid_from_ms") or 0) <= 0 and binding.get("status") in {"active", "ended"}


def _validate_ref(ref: Any, hashes: Mapping[str, Any], period: tuple[int, int]) -> None:
    if not isinstance(ref, Mapping):
        raise ValueError("malformed attribution evidence reference")
    name, digest, locator, at_ms = ref.get("input"), ref.get("sha256"), ref.get("locator"), ref.get("time_ms")
    if (not isinstance(name, str) or hashes.get(name) != digest or not isinstance(digest, str)
            or _HASH.fullmatch(digest) is None or not _text(ref.get("source_id"))
            or not ((isinstance(locator, str) and locator.strip()) or isinstance(locator, Mapping) and locator)
            or not _timestamp_or_none(at_ms) or not period[0] <= at_ms < period[1]):
        raise ValueError("attribution evidence reference mismatch")


def _identifier(value: Any) -> Identifier:
    if isinstance(value, Identifier):
        return value
    if not isinstance(value, Mapping):
        raise ValueError("malformed typed identifier")
    try:
        return Identifier(channel=value["channel"], kind=value["kind"], namespace=value["namespace"], value=value["value"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("malformed typed identifier") from exc


def _identifier_key(value: Identifier) -> tuple[str, str, str, str]:
    return value.channel, value.kind, str(value.namespace), value.value


def _binding_key(row: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return tuple(str(row.get(key) or "") for key in ("channel", "kind", "namespace", "value"))  # type: ignore[return-value]


def _same_identifier(value: Any, expected: Identifier) -> bool:
    try:
        return _identifier_key(_identifier(value)) == _identifier_key(expected)
    except (TypeError, ValueError):
        return False


def _canonical(value: Any, ids: Mapping[str, str]) -> str:
    value = str(value or "")
    return ids.get(value, value)


def _authoritative(binding: Mapping[str, Any]) -> bool:
    return bool(int(binding.get("mapping_verified") or 0)) and _text(binding.get("evidence_ref"))


def _result(status: str, person: str | None = None, binding_id: Any = None,
            basis: str | None = None, refs: list[dict[str, Any]] | None = None,
            reason: str | None = None) -> dict[str, Any]:
    return {"status": status, "canonical_person_id": person, "binding_id": binding_id,
            "basis": basis, "evidence_refs": refs or [], "reason": reason}


def _period(value: Any) -> tuple[int, int]:
    if not isinstance(value, Mapping) or not _valid_period(value.get("start_ms"), value.get("end_ms")):
        raise ValueError("malformed attribution evidence period")
    return int(value["start_ms"]), int(value["end_ms"])


def _valid_period(start: Any, end: Any) -> bool:
    return _timestamp_or_none(start) and _timestamp_or_none(end) and start < end


def _timestamp(value: Any) -> int:
    if not _timestamp_or_none(value):
        raise ValueError("malformed attribution evidence timestamp")
    return value


def _timestamp_or_none(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _ref_key(ref: Mapping[str, Any]) -> tuple[Any, ...]:
    return ref.get("input"), ref.get("source_id"), ref.get("sha256"), str(ref.get("locator")), ref.get("time_ms")
