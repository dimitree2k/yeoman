"""Offline cutover adapters. Inputs are preserved proofs, never current-proof defaults."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import asdict
from typing import Any

from yeoman_gateway.history.layer1 import row_sha256
from yeoman_gateway.history.queries import HistoryQueries
from yeoman_gateway.knowledge._history_sources import build_history_source_aliases
from yeoman_gateway.knowledge.models import SourceRef

_STATE_FIELDS = ("channel", "chat_id", "native_message_id", "direction", "sent_ms",
                 "time_certainty", "text", "current_text", "media_json", "reply_to_native_id",
                 "mentions_json", "provenance", "deleted")


def _source(row: Mapping[str, Any]) -> SourceRef:
    return SourceRef(**{key: row[key] for key in SourceRef.__dataclass_fields__})


def _state(queries: HistoryQueries, row: Mapping[str, Any]) -> dict[str, Any]:
    result = {key: row[key] for key in _STATE_FIELDS}
    for key in ("media_json", "mentions_json"):
        result[key] = json.loads(result[key]) if result[key] is not None else None
    events = queries._rows(
        "SELECT event_id,native_event_id,kind,occurred_ms,time_certainty,provenance,payload_json"
        " FROM message_events WHERE target_message_id=? AND kind IN ('edit','delete')"
        " ORDER BY occurred_ms,event_id", (row["message_id"],))
    for event in events:
        event["payload_json"] = json.loads(event["payload_json"])
    result["events"] = events
    return result


def prepare_legacy_alias_inputs(*, queries: HistoryQueries,
    legacy_rows: Iterable[Mapping[str, Any]],
    preserved_rows: Iterable[Mapping[str, Any]]) -> tuple[
        list[dict[str, Any]], dict[tuple[str, int], tuple[str, ...]], dict[str, int]]:
    """Join issued keys to preserved six-field refs and complete original message state.

    Preserved envelope: event_id/revision, original {issued, state, created_ms},
    origin {store,path,table,row_key,row_sha256}, source_ref (conversion segment ref).
    State contains every _STATE_FIELDS field plus the original ordered edit/delete
    events. Missing state/proof stays withheld. Neither text nor timestamps match
    locators; only (channel, chat, native ID) does.
    """
    originals: dict[tuple[str, int], list[Mapping[str, Any]]] = defaultdict(list)
    candidate_copies = 0
    for row in preserved_rows:
        key = row["event_id"], row["revision"]
        originals[key].append(row)
        candidate_copies += 1
    distinct: dict[tuple[str, int], dict[str, Any]] = {}
    for item in legacy_rows:
        row = dict(item)
        key = _source(row).key
        if key in distinct and distinct[key] != row:
            raise ValueError("conflicting_legacy_key")
        distinct[key] = row
    counts = Counter({key: 0 for key in (
        "mapped", "missing", "ambiguous", "changed", "purged_revoked", "other_channel")})
    prepared, locators = [], {}
    for key, row in sorted(distinct.items()):
        source = _source(row)
        status = "missing"
        targets: set[str] = set()
        proofs = []
        states = []
        if source.channel != "whatsapp":
            status = "other_channel"
        elif row.get("status") in ("revoked", "purged"):
            status = "purged_revoked"
        elif "source_audience_json" not in row:
            status = "missing"
        else:
            invalid = False
            for preserved in originals.get(key, ()):
                original, origin = preserved.get("original"), preserved.get("origin")
                if (not isinstance(original, dict) or not isinstance(origin, dict)
                        or not all(origin.get(k) for k in ("store", "path", "table", "row_key"))
                        or origin.get("row_sha256") != row_sha256(original)
                        or not preserved.get("source_ref")):
                    invalid = True
                    continue
                state = original.get("state")
                issued = original.get("issued")
                if (not isinstance(state, dict) or not isinstance(issued, dict)
                        or not all(k in state for k in (*_STATE_FIELDS, "events"))
                        or not state["native_message_id"]):
                    invalid = True
                    continue
                states.append((issued, state))
                matches = queries._rows(
                    "SELECT * FROM messages_current WHERE channel=? AND chat_id=? AND native_message_id=?",
                    (state["channel"], state["chat_id"], state["native_message_id"]))
                targets.update(match["message_id"] for match in matches)
                proofs.append({"origin": {k: origin[k] for k in ("store", "path", "table", "row_key", "row_sha256")}, "source_ref": preserved["source_ref"],
                               "native_locator": [state["channel"], state["chat_id"], state["native_message_id"]]})
            if len(targets) > 1 or len({row_sha256(state) for state in states}) > 1:
                status = "ambiguous"
            elif not invalid and len(targets) == 1 and states:
                mid = next(iter(targets))
                current = queries._rows("SELECT * FROM messages_current WHERE message_id=?", (mid,))[0]
                if current["deleted"]:
                    status = "purged_revoked"
                elif states[0][0] != asdict(source) or states[0][1] != _state(queries, current):
                    status = "changed"
                else:
                    # Only after independent complete-state equality can history supply its hash.
                    row["content_fingerprint"] = queries.content_fingerprint(mid)
                    row["native_id"] = current["native_message_id"]
                    locators[key] = (mid,)
                    aliases, _ = build_history_source_aliases(queries=queries, legacy_rows=[row], locators=locators)
                    if key in aliases:
                        status = "mapped"
                        row["message_id"] = mid
                        order_values = {p["original"].get("created_ms") for p in originals[key]}
                        if len(order_values) == 1 and type(next(iter(order_values))) is int:
                            row["created_ms"] = next(iter(order_values))
                    else:
                        status = "missing"
        if status != "mapped":
            locators.pop(key, None)
            row.pop("content_fingerprint", None)
        row["cutover_status"] = status
        row["preserved_proofs"] = sorted(
            {row_sha256(p): p for p in proofs}.values(), key=row_sha256)
        prepared.append(row)
        counts[status] += 1
    counts["total"] = len(distinct)
    counts["candidate_copies"] = candidate_copies
    return prepared, locators, dict(counts)


def native_prefix(queries: HistoryQueries) -> dict[str, dict[str, Any]]:
    """Native physical refs must be positive and inside this exact committed vector."""
    vector = {s.relative_path: s.line_number for s in queries.snapshot.sources}
    result = {}
    for row in queries._rows("SELECT * FROM messages_current WHERE channel='whatsapp'"):
        refs = json.loads(row["source_refs"])
        native = []
        for ref in refs:
            path, _, number = ref.rpartition("#")
            if path.startswith("whatsapp/"):
                if not number.isdecimal() or int(number) < 1:
                    raise ValueError("invalid_native_capture_ref")
                native.append((path, int(number)))
        if any(number <= vector.get(path, 0) for path, number in native):
            result[row["message_id"]] = row
    return result


def prepare_capture_inputs(*, queries: HistoryQueries,
    legacy_boundary: tuple[int, str], legacy_rows: Iterable[Mapping[str, Any]],
    jobs: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Accept proven completion or preserved created/event ordering, never provider time.

    Ordering proof pins progress as 'boundary' and the separately preserved
    initial selection boundary as 'forward_start'. A moving cursor alone cannot
    prove historical exclusion. For messages
    without an issuable source, ordering still supplies refs-only pending/exclusion.
    Permanent refusals are validated again against loaded policy by the producer.
    """
    rows: dict[str, Mapping[str, Any]] = {}
    refs: dict[tuple[str, int], tuple[str, SourceRef]] = {}
    for row in legacy_rows:
        mid = row.get("message_id")
        if not mid and row.get("cutover_status") == "other_channel":
            mid = row["event_id"]
        if not mid:
            continue
        if mid in rows:
            previous = rows[mid]
            if any(previous[k] != row[k] for k in previous.keys() & row.keys()):
                raise ValueError("conflicting_capture_proof")
            row = {**previous, **row}
        rows[mid] = row
        if "source" in row:
            source = SourceRef(**row["source"])
        elif row.get("cutover_status") in ("mapped", "other_channel"):
            source = _source(row)
        else:
            continue
        if source.key in refs and refs[source.key] != (mid, source):
            raise ValueError("conflicting_capture_proof")
        refs[source.key] = mid, source
    job_mids = set()
    for job in jobs:
        for ref in json.loads(job["sources_json"]):
            key = ref["event_id"], ref["revision"]
            if key in refs:
                if refs[key][1].channel == "whatsapp":
                    job_mids.add(refs[key][0])
            elif ref.get("channel", "whatsapp") == "whatsapp":
                raise ValueError("unmapped_handover_job")
    pending, processed, classifications = [], [], {}
    sources_by_mid = {mid: source for mid, source in refs.values() if source.channel == "whatsapp"}
    prefix = native_prefix(queries)
    for mid, current in prefix.items():
        row = rows.get(mid, {})
        source = sources_by_mid.get(mid)
        if mid in job_mids:
            if source is None:
                raise ValueError("unmapped_handover_job")
            continue
        reason = ("not_inbound" if current["direction"] != "in" else
                  "derived_only" if current["provenance"] == "derived_only" else
                  "source_revoked" if current["deleted"] else
                  "empty_text" if not (current["current_text"] or "").strip() else "")
        if row.get("classification") == "not_policy_chat":
            reason = "not_policy_chat"
        if reason:
            classifications[mid] = reason
        elif row.get("completed") is True and source is not None:
            processed.append(source)
        elif (row.get("boundary") == list(legacy_boundary)
              and type(row.get("created_ms")) is int and isinstance(row.get("event_id"), str)):
            order = row["created_ms"], row["event_id"]
            start = row.get("forward_start")
            if order <= legacy_boundary:
                if (not isinstance(start, list) or len(start) != 2
                        or type(start[0]) is not int or not isinstance(start[1], str)
                        or not order <= tuple(start) <= legacy_boundary):
                    raise ValueError("unclassified_handover_message")
                classifications[mid] = "historical_not_selected"
            elif source is not None:
                pending.append(source)
            else:
                classifications[mid] = "pending"
        else:
            raise ValueError("unclassified_handover_message")
    # Proven mapped backfill assignments/jobs stay admissible without forward eligibility.
    for mid, source in sources_by_mid.items():
        if mid not in prefix and mid not in job_mids:
            if rows[mid].get("completed") is True:
                processed.append(source)
            elif (rows[mid].get("boundary") == list(legacy_boundary)
                  and type(rows[mid].get("created_ms")) is int
                  and (rows[mid]["created_ms"], rows[mid]["event_id"]) > legacy_boundary):
                pending.append(source)
            else:
                raise ValueError("unclassified_handover_message")
    return {"pending": tuple(sorted(pending, key=lambda s: s.key)),
            "processed": tuple(sorted(processed, key=lambda s: s.key)),
            "classifications": dict(sorted(classifications.items()))}
