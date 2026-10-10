"""Offline cutover adapters. Inputs are preserved proofs, never current-proof defaults."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict
from typing import Any

from yeoman_gateway.history.layer1 import row_sha256
from yeoman_gateway.history.queries import HistoryQueries
from yeoman_gateway.knowledge._history_sources import (
    _author_contact,
    _canonical_author,
    build_history_source_aliases,
    is_legacy_node,
)
from yeoman_gateway.knowledge.models import KnowledgeError, SourceRef

_STATE_FIELDS = ("channel", "chat_id", "native_message_id", "direction", "sent_ms",
                 "time_certainty", "text", "current_text", "media_json", "reply_to_native_id",
                 "mentions_json", "provenance", "deleted")


def _source(row: Mapping[str, Any]) -> SourceRef | None:
    try:
        return SourceRef(**{key: row[key] for key in SourceRef.__dataclass_fields__})
    except (KnowledgeError, KeyError, TypeError, ValueError):
        return None


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
    """Join issued keys to hash-bound originals, checking only recorded fields.

    Preserved envelope: event_id/revision, original {issued, state, created_ms},
    origin {store,path,table,row_key,row_sha256}, source_ref (conversion segment ref).
    The union of recorded states requires locator and original text; absent fields stay unrecorded.
    Missing identity/proof stays withheld. Neither text nor timestamps match
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
        source = _source(row)
        key = source.key if source is not None else (row.get('event_id'), row.get('revision'))
        if key in distinct and distinct[key] != row:
            raise ValueError("conflicting_legacy_key")
        distinct[key] = row
    counts = Counter({key: 0 for key in (
        "mapped", "missing", "ambiguous", "changed", "purged_revoked", "other_channel", "legacy_node",
        "author_unresolved", "author_different_contact")})
    prepared, locators = [], {}
    for key, row in sorted(distinct.items(), key=lambda item: (str(item[0][0]),
            (0, item[0][1]) if type(item[0][1]) is int else (1, str(item[0][1])))):
        source = _source(row)
        status = "missing"
        reason = "no_preserved_original"
        targets: set[str] = set()
        target_times: dict[str, Any] = {}
        proofs = []
        states = []
        contacts = {}
        message_time = None
        def author(value):
            # Never cache a lookup made before this key's message time is known.
            key = (row_sha256(value), message_time if type(message_time) is int else None)
            if key not in contacts:
                contacts[key] = (_author_contact(queries, value, at_ms=message_time)
                                 if type(message_time) is int else None)
            if contacts[key] is not None:
                return ('contact', contacts[key])
            canonical = _canonical_author(value)
            return ('unresolved', canonical if isinstance(canonical, str) else row_sha256(canonical))
        def author_reason(values):
            return 'author_unresolved' if any(v[0]=='unresolved' for v in values) else 'author_different_contact'

        if is_legacy_node(row.get("event_id", "")):
            status = reason = "legacy_node"
        elif source is None:
            principal = row.get('author_principal')
            row['cutover_reason'] = ('unissued_principal' if isinstance(principal, str) and not principal.strip()
                                     else 'invalid_source_ref')
        elif source.channel != "whatsapp":
            status = "other_channel"
            reason = "other_channel"
        elif row.get("status") in ("revoked", "purged"):
            status = "purged_revoked"
            reason = "purged_revoked"
        elif "source_audience_json" not in row:
            reason = "no_audience_proof"
        else:
            invalid = False
            for preserved in originals.get(key, ()):
                original, origin = preserved.get("original"), preserved.get("origin")
                proof_bytes = preserved.get("preserved_original", original)
                envelope_valid = ("preserved_original" not in preserved or
                    preserved.get("envelope_sha256") == row_sha256(original))
                if (not isinstance(original, dict) or not isinstance(origin, dict)
                        or not all(origin.get(k) for k in ("store", "path", "table", "row_key"))
                        or origin.get("row_sha256") != row_sha256(proof_bytes)
                        or not envelope_valid
                        or not preserved.get("source_ref")):
                    invalid = True
                    continue
                state = original.get("state")
                issued = original.get("issued")
                if not isinstance(state, dict) or (issued is not None and not isinstance(issued, dict)):
                    invalid = True
                    continue
                # Comparison views never alter the hash-bound originals or issued refs.
                state = dict(state)
                if state.get('author_principal') in (None, ''):
                    state.pop('author_principal', None)
                if state.get('reply_to_native_id') in (None, ''):
                    state.pop('reply_to_native_id', None)
                if issued is not None:
                    issued = dict(issued)
                states.append((issued, state))
                if all(state.get(k) for k in ("channel", "chat_id", "native_message_id")):
                    matches = queries._rows(
                        "SELECT * FROM messages_current WHERE channel=? AND chat_id=? AND native_message_id=?",
                        (state["channel"], state["chat_id"], state["native_message_id"]))
                    targets.update(match["message_id"] for match in matches)
                    target_times.update((match["message_id"], match["sent_ms"]) for match in matches)
                proofs.append({"origin": {k: origin[k] for k in ("store", "path", "table", "row_key", "row_sha256")}, "source_ref": preserved["source_ref"],
                               "native_locator": [state.get(k) for k in ("channel", "chat_id", "native_message_id")]})
            times = {state['sent_ms'] for _,state in states if type(state.get('sent_ms')) is int}
            message_time = (target_times.get(next(iter(targets))) if len(targets)==1
                            else next(iter(times)) if len(times)==1 else None)
            for issued,state in states:
                if state.get('author_principal') not in (None, ''):  # null/empty = not recorded
                    state['author_principal'] = author(state['author_principal'])
                if issued is not None and 'author_principal' in issued:
                    issued['author_principal'] = author(issued['author_principal'])
            conflicts = any((left_issued is not None and right_issued is not None and left_issued != right_issued) or any(
                left[k] != right[k] for k in left.keys() & right.keys())
                for i,(left_issued,left) in enumerate(states) for right_issued,right in states[i+1:])
            author_values = {state['author_principal'] for _,state in states if state.get('author_principal') not in (None, '')}
            author_values.update(issued['author_principal'] for issued,_ in states if issued is not None and 'author_principal' in issued)
            if len(author_values)>1:
                row['author_reason'] = author_reason(author_values)
            recorded = {k: v for _, state in states for k, v in state.items()}
            if not conflicts and all(recorded.get(k) for k in ("channel", "chat_id", "native_message_id")) and not targets:
                matches = queries._rows(
                    "SELECT message_id FROM messages_current WHERE channel=? AND chat_id=? AND native_message_id=?",
                    (recorded["channel"], recorded["chat_id"], recorded["native_message_id"]))
                targets.update(match["message_id"] for match in matches)
            if len(targets) > 1 or conflicts:
                status, reason = "ambiguous", "ambiguous_locator"
            elif invalid:
                reason = "incomplete_original_proof"
            elif not targets and states:
                reason = "locator_not_in_history"
            elif len(targets) == 1 and states:
                mid = next(iter(targets))
                current = queries._rows("SELECT * FROM messages_current WHERE message_id=?", (mid,))[0]
                expected = _state(queries, current)
                # History already resolved this sender; its terminal contact is the authority.
                expected["author_principal"] = (("contact", current["sender_contact_id"]) if current.get("sender_contact_id")
                                                else author(current["sender_identifier"] or None))
                # An original observation with no mutations proves pre-edit text.
                if not recorded.get("events"):
                    expected["events"] = []
                    expected["current_text"] = expected["text"]
                issued_proofs = [issued for issued, _ in states if issued is not None]
                complete = all(k in recorded for k in ("text", "sent_ms", "time_certainty"))
                if current["deleted"]:
                    status, reason = "purged_revoked", "purged_revoked"
                elif not issued_proofs or not complete:
                    reason = "no_author_or_text_proof"
                elif any(value[0]=='unresolved' for value in [*author_values, author(source.author_principal), expected['author_principal']]):
                    status, reason = 'changed', 'author_mismatch'
                elif issued_proofs[0] != dict(asdict(source), author_principal=author(source.author_principal)):
                    status, reason = "changed", ("author_mismatch" if issued_proofs[0].get("author_principal") != author(source.author_principal)
                        else "time_mismatch" if issued_proofs[0].get("occurred_at_ms") != source.occurred_at_ms
                        else "issued_source_mismatch")
                elif recorded["text"] != expected["text"]:
                    status, reason = "changed", "text_mismatch"
                elif any(k not in expected or expected[k] != value for k,value in recorded.items()):
                    status, reason = "changed", ("author_mismatch" if recorded.get("author_principal", expected["author_principal"]) != expected["author_principal"]
                        else "recorded_field_mismatch")
                else:
                    row["content_fingerprint"] = queries.content_fingerprint(mid)
                    row["native_id"] = current["native_message_id"]
                    locators[key] = (mid,)
                    aliases, alias_counts = build_history_source_aliases(queries=queries, legacy_rows=[row], locators=locators)
                    if key in aliases:
                        status, reason = "mapped", "mapped"
                        row["message_id"] = mid
                        row["proven_fields"] = sorted([*recorded, "issued", "original_row_sha256"])
                        order_values = {p["original"].get("created_ms") for p in originals[key]}
                        if len(order_values) == 1 and type(next(iter(order_values))) is int:
                            row["created_ms"] = next(iter(order_values))
                    else:
                        principal = (("contact", current["sender_contact_id"]) if current.get("sender_contact_id")
                                     else author(current["sender_identifier"] or None))
                        reason = ("author_mismatch" if author(source.author_principal) != principal else
                                  "time_mismatch" if source.occurred_at_ms != current["sent_ms"] else
                                  next((code for code in ("audience_unproven", "audience_mismatch", "history_sender_contact_unproven", "legacy_author_unresolvable", "legacy_author_contact_unproven",
                                      "author_contact_mismatch", "fingerprint_mismatch", "source_unproven", "chat_mismatch")
                                      if alias_counts.get(code)), "no_author_or_audience_proof"))
        if reason == 'author_mismatch':
            row['author_reason'] = author_reason([*author_values,author(source.author_principal),expected['author_principal']])
        if 'author_reason' in row:
            counts[row['author_reason']] += 1
        if status != "mapped":
            locators.pop(key, None)
            row.pop("content_fingerprint", None)
        row.setdefault("cutover_reason", reason)
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
    jobs: Iterable[Mapping[str, Any]], summary: dict[str, Any] | None = None,
    permanent_reason: Callable[[dict[str, Any]], str] | None = None) -> dict[str, Any]:
    """Accept proven completion or preserved created/event ordering, never provider time.

    Ordering proof pins progress as 'boundary' and the separately preserved
    initial selection boundary as 'forward_start'. A moving cursor alone cannot
    prove historical exclusion. For messages
    without an issuable source, ordering still supplies refs-only pending/exclusion.
    Permanent refusals are validated again against loaded policy by the producer.
    """
    rows: dict[str, Mapping[str, Any]] = {}
    refs: dict[tuple[str, int], tuple[str, SourceRef]] = {}
    sources_by_mid: dict[str, SourceRef] = {}
    observations: dict[str, set[tuple[int, str]]] = defaultdict(set)
    source_rows: dict[tuple[str, int], Mapping[str, Any]] = {}
    duplicate_mapped_sources = historical_backfill_aliases = 0
    for row in legacy_rows:
        mid = row.get("message_id")
        if not mid and row.get("cutover_status") == "other_channel":
            mid = row["event_id"]
        if not mid:
            continue
        # Source identity belongs to the issued row, not its observation ordering.
        source = (_source(row['source']) if 'source' in row else
                  _source(row) if row.get('cutover_status') in ('mapped', 'other_channel') else None)
        order = (row.get('created_ms'), row.get('event_id'))
        if type(order[0]) is int and isinstance(order[1], str):
            observations[mid].add(order)
        new_source = source is not None and source.key not in refs
        if source is not None:
            if source.key in refs and refs[source.key] != (mid, source):
                raise ValueError('conflicting_capture_proof')
            previous_row = source_rows.get(source.key)
            if previous_row is not None and any(previous_row[k] != row[k]
                    for k in (previous_row.keys() & row.keys()) - {'event_id', 'created_ms'}):
                raise ValueError('conflicting_capture_proof')
            source_rows[source.key] = row
        previous_source = sources_by_mid.get(mid)
        if (source is not None and previous_source is not None and previous_source.key != source.key
                and row.get('cutover_status') == 'mapped' and rows[mid].get('cutover_status') == 'mapped'):
            # Two legacy keys (e.g. a wa_ journal ID and the native ID) both proven for one
            # message: both stay aliases; capture progress uses one preferred proof.
            current = queries.message(mid)
            if current is None or not current['sender_contact_id']:
                raise ValueError('conflicting_capture_proof')
            contact = queries.terminal(current['sender_contact_id'])
            for candidate, issued in ((rows[mid], previous_source), (row, source)):
                owner = candidate.get('author_contact_id')
                if (not owner or queries.terminal(str(owner)) != contact
                        or _author_contact(queries, issued.author_principal, at_ms=current['sent_ms'],
                                           time_basis=current['time_certainty']) != contact
                        or (issued.channel, issued.chat_id, issued.occurred_at_ms) !=
                           (current['channel'], current['chat_id'], current['sent_ms'])):
                    raise ValueError('conflicting_capture_proof')
            def rank(candidate):
                order = (candidate.get('created_ms'), candidate.get('event_id'))
                return (candidate.get('completed') is not True,
                        order if type(order[0]) is int and isinstance(order[1], str) else (2**63, ''))
            preferred = min((rows[mid], row), key=rank)
            refs[source.key] = mid, source
            rows[mid] = preferred
            sources_by_mid[mid] = source if preferred is row else previous_source
            duplicate_mapped_sources += int(new_source)
            continue
        if mid in rows:
            previous = rows[mid]
            if any(previous[k] != row[k] for k in (previous.keys() & row.keys()) - {'event_id', 'created_ms'}):
                raise ValueError("conflicting_capture_proof")
            previous_order = (previous.get('created_ms'), previous.get('event_id'))
            different_order = any(previous[k] != row[k] for k in (previous.keys() & row.keys()) & {'event_id', 'created_ms'})
            if different_order and (type(previous_order[0]) is not int or not isinstance(previous_order[1], str)
                                    or type(order[0]) is not int or not isinstance(order[1], str)):
                raise ValueError('conflicting_capture_proof')
            row = {**previous, **row}
            if different_order:
                created, event = min(previous_order, order)
                row = dict(row, created_ms=created, event_id=event)
        rows[mid] = row
        if source is None:
            continue
        if mid in sources_by_mid and sources_by_mid[mid] != source:
            raise ValueError('conflicting_capture_proof')
        sources_by_mid[mid] = source
        if source.key in refs and refs[source.key] != (mid, source):
            raise ValueError("conflicting_capture_proof")
        refs[source.key] = mid, source
    job_mids = set()
    unmapped_terminal = 0
    no_legacy_row_pending = 0
    for job in jobs:
        for ref in json.loads(job["sources_json"]):
            if is_legacy_node(ref["event_id"]):
                continue
            key = ref["event_id"], ref["revision"]
            if key in refs:
                if refs[key][1].channel == "whatsapp":
                    job_mids.add(refs[key][0])
            elif ref.get("channel", "whatsapp") == "whatsapp":
                if job.get("state") in ("queued", "running", "failed") or (
                        job.get("state") == "skipped" and job.get("reason") == "queue_full"):
                    raise ValueError("unmapped_handover_job")
                unmapped_terminal += 1
    pending, processed, classifications = [], [], {}
    sources_by_mid = {mid: source for mid, source in sources_by_mid.items() if source.channel == "whatsapp"}
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
        if permanent_reason is not None:
            reason = permanent_reason(current)
        elif row.get("classification") == "not_policy_chat":
            reason = "not_policy_chat"
        if reason:
            classifications[mid] = reason
        elif mid not in rows:
            classifications[mid] = "pending"
            no_legacy_row_pending += 1
        elif row.get("completed") is True and source is not None:
            processed.append(source)
        elif (row.get("boundary") == list(legacy_boundary)
              and type(row.get("created_ms")) is int and isinstance(row.get("event_id"), str)):
            order = row["created_ms"], row["event_id"]
            start = row.get("forward_start")
            if order <= legacy_boundary:
                if (not isinstance(start, list) or len(start) != 2
                        or type(start[0]) is not int or not isinstance(start[1], str)
                        or not tuple(start) <= legacy_boundary):
                    raise ValueError("unclassified_handover_message")
                if order <= tuple(start):
                    classifications[mid] = "historical_not_selected"
                elif source is not None:
                    pending.append(source)
                else:
                    classifications[mid] = "pending"
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
                # Backfill-only aliases need no forward capture assignment.
                historical_backfill_aliases += 1
    if summary is not None:
        summary.update(duplicate_mapped_sources=duplicate_mapped_sources, historical_backfill_aliases=historical_backfill_aliases, no_legacy_row_pending=no_legacy_row_pending, unmapped_terminal_job_refs=unmapped_terminal, duplicate_observations=sum(max(0, len(values)-1) for values in observations.values()),
            observations={mid: [dict(created_ms=created,event_id=event) for created,event in sorted(values)]
                          for mid,values in sorted(observations.items()) if len(values)>1})
    return {"pending": tuple(sorted(pending, key=lambda s: s.key)),
            "processed": tuple(sorted(processed, key=lambda s: s.key)),
            "classifications": dict(sorted(classifications.items()))}
