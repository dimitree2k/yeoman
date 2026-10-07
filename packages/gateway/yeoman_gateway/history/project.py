"""Build history.db from Layer 1 alone (spec: Projection). Always a full, deterministic rebuild."""

from __future__ import annotations

import bisect
import hashlib
import json
import os
import sqlite3
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yeoman_shared.raw_archive.paths import is_protected

from .attestations import Attestation, message_copy_id, resolve_author_targets
from .extract import EventCopy, Extracted, MessageCopy, extract
from .ids import Ident, classify
from .layer1 import canonical_json, iter_layer1, layer1_files
from .resolve import Resolution, resolve
from .schema import PROJECTOR_VERSION, create

WINDOW_MS = 120_000
_CERTAINTY = {"native": 0, "provider_timestamp": 0, "capture_time_approx": 1, "unknown": 2}
_PROVENANCE = {"native": 0, "recovered_text": 1, "verbatim_unverified": 2, "derived_only": 3}
Key = tuple[str, str, str]


@dataclass(frozen=True)
class AuthorCorrection:
    attestation: Attestation
    contact_id: str | None


def project(roots: Sequence[Path], db_path: Path) -> dict[str, Any]:
    if is_protected(db_path):
        raise PermissionError(f"refusing to write history.db into the raw archive: {db_path}")
    files = layer1_files(roots)
    ex = extract(iter_layer1(roots))
    res = resolve(ex.identity)
    arvid = res.role_contact.get("assistant")
    authors = _authors(ex, res)
    messages, unattached = _messages(ex, res, arvid, authors)
    events = _events(ex, res, arvid, {m["message_id"] for m in messages}, res.review, authors)
    line_counts = _write(db_path, res, messages, events, files)
    return _report(ex, res, messages, events, line_counts, unattached)


def _authors(ex: Extracted, res: Resolution) -> dict[str, AuthorCorrection]:
    res.review.update(ex.review)
    winners, review = resolve_author_targets(ex.attestations, ex.messages, ex.events)
    copies = {copy.ref: copy for copy in [*ex.messages, *ex.events]}
    authors: dict[str, AuthorCorrection] = {}
    unresolved: dict[str, dict[str, Any]] = {}
    for ref, att in winners.items():
        copy = copies[ref]
        contact, _ = res.resolve(classify(att.fields["anchor"]), occurred_ms=copy.occurred_ms,
                                 time_basis=copy.time_certainty)
        authors[ref] = AuthorCorrection(att, contact)
        if contact is None:
            item = unresolved.setdefault(att.ref, {
                "reason": "unresolved_author_anchor", "attestation_ref": att.ref,
                "anchor": att.fields["anchor"], "targets": []})
            item["targets"].append({"source_ref": ref, "occurred_ms": copy.occurred_ms,
                                    "time_basis": copy.time_certainty})
    review.extend(unresolved.values())
    invalid = {r["attestation_ref"] for r in review if "attestation_ref" in r}
    for att in ex.attestations:
        if att.ref in invalid:
            file = att.ref.split("#", 1)[0]
            ex.outcomes[(file, "attestation")] -= 1
            ex.count(att.ref, "skipped:invalid_author_target")
    res.review["author_targets"] = sorted(review, key=canonical_json)
    keyed: dict[str, list[MessageCopy]] = defaultdict(list)
    for copy in ex.messages:
        if copy.native_id:
            keyed[message_copy_id(copy)].append(copy)
    res.review["message_id_collisions"] = [
        {"message_id": key, "copies": [
            {"source_ref": c.ref, "text": c.text,
             "attestation_ref": authors[c.ref].attestation.ref if c.ref in authors else None}
            for c in sorted(copies, key=_order)]}
        for key, copies in sorted(keyed.items())
        if len({c.text for c in copies if c.text is not None}) > 1]
    return authors


def _order(copy: MessageCopy | EventCopy) -> tuple[int, str]:
    return copy.rank, copy.ref


def _best_time(copies: Sequence[MessageCopy | EventCopy]) -> tuple[int | None, str]:
    timed = [c for c in copies if c.occurred_ms is not None]
    if not timed:
        return None, "unknown"
    best = min(timed, key=lambda c: (_CERTAINTY.get(c.time_certainty, 2), c.rank, c.ref))
    return best.occurred_ms, best.time_certainty


def _direction(copies: Sequence[MessageCopy]) -> str:
    return "out" if any(c.from_assistant or c.direction == "out" for c in copies) else "in"


def _messages(ex: Extracted, res: Resolution, arvid: str | None,
              authors: dict[str, AuthorCorrection]) -> tuple[list[dict[str, Any]], int]:
    keyed: dict[Key, list[MessageCopy]] = defaultdict(list)
    loose: list[MessageCopy] = []
    for copy in ex.messages:
        if copy.native_id:
            keyed[(copy.channel, copy.chat_id, copy.native_id)].append(copy)
        elif copy.batch_key:
            keyed[(copy.channel, copy.chat_id, "\0batch:" + copy.batch_key)].append(copy)
        else:
            loose.append(copy)
    for copies in keyed.values():
        copies.sort(key=_order)
    text_of = {k: next((c.text for c in v if c.text is not None), None) for k, v in keyed.items()}
    index: dict[tuple[str, str, str], tuple[list[int], list[Key]]] = {}
    for key in sorted(keyed):
        if key[2].startswith("\0batch:"):
            continue
        ms, _ = _best_time(keyed[key])
        if ms is not None:
            times, keys = index.setdefault((key[0], key[1], _direction(keyed[key])), ([], []))
            times.append(ms)
            keys.append(key)
    for slot, (times, keys) in list(index.items()):
        pairs = sorted(zip(times, keys, strict=True))
        index[slot] = ([t for t, _ in pairs], [k for _, k in pairs])

    joins: dict[str, Key] = {}
    purged_claims: dict[Key, set[str | None]] = defaultdict(set)
    for copy in sorted(loose, key=_order):
        target, via_purged = _join_target(copy, index, text_of)
        if target is not None:
            joins[copy.ref] = target
            if via_purged:
                purged_claims[target].add(copy.text)
    standalone: list[MessageCopy] = []
    for copy in sorted(loose, key=_order):
        target = joins.get(copy.ref)
        if target is not None and len(purged_claims.get(target, {copy.text})) <= 1:
            keyed[target].append(copy)
        else:
            standalone.append(copy)

    rows = [_message_row(k[0], k[1], None if k[2].startswith("\0batch:") else k[2],
                         sorted(keyed[k], key=_order), res, arvid, authors,
                         stable_key=k[2][len("\0batch:"):] if k[2].startswith("\0batch:") else None)
            for k in sorted(keyed)]
    rows += [_message_row(c.channel, c.chat_id, None, [c], res, arvid, authors) for c in standalone]
    _name_fallback(rows)
    unattached = _attach_media(rows, ex)
    rows.sort(key=lambda r: r["message_id"])
    return rows, unattached


def _join_target(copy: MessageCopy, index: dict[tuple[str, str, str], tuple[list[int], list[Key]]],
                 text_of: dict[Key, str | None]) -> tuple[Key | None, bool]:
    if copy.occurred_ms is None or copy.text is None:
        return None, False
    slot = index.get((copy.channel, copy.chat_id, _direction([copy])))
    if slot is None:
        return None, False
    times, keys = slot
    low = bisect.bisect_left(times, copy.occurred_ms - WINDOW_MS)
    high = bisect.bisect_right(times, copy.occurred_ms + WINDOW_MS)
    candidates = keys[low:high]
    exact = [k for k in candidates if text_of[k] == copy.text]
    if len(exact) == 1:
        return exact[0], False
    if exact:
        return None, False
    purged = [k for k in candidates if text_of[k] is None]
    return (purged[0], True) if len(purged) == 1 else (None, False)


def _basis(res: Resolution, ident: Ident | None, provenance: str,
           inferred: bool, occurred_ms: int | None, time_basis: str) -> tuple[str | None, str]:
    contact, match = res.resolve(ident, occurred_ms=occurred_ms, time_basis=time_basis)
    if contact is None:
        return None, "unknown"
    if provenance != "native" or inferred:
        return contact, "derived_claim"
    if match == "numeric_match":
        return contact, "numeric_match"
    return contact, "native_identifier"


def _text_and_provenance(copies: Sequence[MessageCopy]) -> tuple[str | None, str]:
    native = [c for c in copies if c.provenance == "native"]
    best_class = min((c.provenance for c in copies), key=lambda p: _PROVENANCE.get(p, 3))
    text_copy = next((c for c in copies if c.text is not None), None)
    if text_copy is None:
        return None, "native" if native else best_class
    if text_copy.provenance == "native":
        return text_copy.text, "native"
    if native:
        agreeing = sum(1 for c in copies if c.provenance != "native" and c.text == text_copy.text)
        return text_copy.text, "recovered_text" if agreeing >= 2 else "verbatim_unverified"
    return text_copy.text, best_class


def _message_row(channel: str, chat: str, native_id: str | None, copies: list[MessageCopy],
                 res: Resolution, arvid: str | None, authors: dict[str, AuthorCorrection],
                 stable_key: str | None = None) -> dict[str, Any]:
    if native_id:
        message_id = f"{channel}:{chat}:{native_id}"
    else:
        identity = stable_key or copies[0].ref
        message_id = f"{channel}:{chat}:derived:{hashlib.sha256(identity.encode()).hexdigest()[:32]}"
    from_assistant = any(c.from_assistant for c in copies)
    sender_copy = next((c for c in copies if c.sender is not None), None)
    sender_identifier = sender_copy.sender_raw if sender_copy is not None and not from_assistant else None
    sent_ms, certainty = _best_time(copies)
    contact: str | None
    correction = max((authors[c.ref] for c in copies if c.ref in authors),
                     key=lambda a: (a.attestation.at_ms, a.attestation.ref), default=None)
    if correction is not None:
        contact, basis = correction.contact_id, "owner_attested"
    elif from_assistant:
        native = any(c.from_assistant and c.provenance == "native" for c in copies)
        contact, basis = arvid, "native_identifier" if native else "derived_claim"
    elif sender_copy is not None:
        contact, basis = _basis(res, sender_copy.sender, sender_copy.provenance, sender_copy.inferred_sender,
                                sent_ms, certainty)
    else:
        contact, basis = None, "unknown"
    if contact is None:
        basis = "unknown"
    text, provenance = _text_and_provenance(copies)
    media = dict(next((c.media for c in copies if c.media), None) or {})
    description = next((c.description for c in copies if c.description), None)
    if description:
        media["description"] = {"text": description, "generator": None, "generated_ms": None,
                                "provenance": "derived_only"}
    return {
        "message_id": message_id, "channel": channel, "chat_id": chat, "native_message_id": native_id,
        "sender_contact_id": contact, "sender_identifier": sender_identifier, "sender_basis": basis,
        "direction": _direction(copies), "sent_ms": sent_ms, "time_certainty": certainty, "text": text,
        "media": media, "reply_to_native_id": next((c.reply_to for c in copies if c.reply_to), None),
        "mentions": next((c.mentions for c in copies if c.mentions), None), "provenance": provenance,
        "source_refs": sorted({r for c in copies for r in (c.ref, *c.extra_refs)}
                              | {authors[c.ref].attestation.ref for c in copies if c.ref in authors}),
        "_sender_name": next((c.sender_name for c in copies if c.sender_name), None),
        "_author_corrected": correction is not None,
    }


def _name_fallback(rows: list[dict[str, Any]]) -> None:
    index: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in rows:
        if row["sender_contact_id"] and row["_sender_name"]:
            index[(row["chat_id"], row["_sender_name"])].add(row["sender_contact_id"])
    for row in rows:
        if (not row["_author_corrected"] and row["sender_contact_id"] is None and row["sender_identifier"] is None
                and row["direction"] == "in" and row["_sender_name"]):
            found = index.get((row["chat_id"], row["_sender_name"]), set())
            if len(found) == 1:
                row["sender_contact_id"], row["sender_basis"] = next(iter(found)), "push_name"


def _attach_media(rows: list[dict[str, Any]], ex: Extracted) -> int:
    by_key = {(r["channel"], r["chat_id"], r["native_message_id"]): r for r in rows if r["native_message_id"]}
    unattached = 0
    for record in sorted(ex.media_records, key=lambda x: x.ref):
        row = by_key.get((record.channel, record.chat_id, record.native_id))
        if row is None:
            unattached += 1
            continue
        for key, value in record.media.items():
            row["media"].setdefault(key, value)
        row["source_refs"] = sorted(set(row["source_refs"]) | {record.ref})
    for desc in sorted(ex.descriptions, key=lambda x: x.ref):
        row = by_key.get((desc.channel, desc.chat_id, desc.native_id))
        if row is None:
            unattached += 1
            continue
        slot = "ocr_text" if desc.mode == "ocr" else "description"
        row["media"].setdefault(slot, {"text": desc.text, "generator": desc.generator,
                                       "generated_ms": desc.generated_ms, "provenance": "derived_only"})
        row["source_refs"] = sorted(set(row["source_refs"]) | {desc.ref})
    return unattached


def _actor(copy: EventCopy, res: Resolution, arvid: str | None,
           correction: AuthorCorrection | None = None) -> tuple[str | None, str]:
    if correction is not None:
        return correction.contact_id, "owner_attested" if correction.contact_id is not None else "unknown"
    if copy.from_assistant:
        contact, basis = arvid, "native_identifier" if copy.provenance == "native" else "derived_claim"
    elif copy.actor is None:
        return None, "unknown"
    else:
        contact, match = res.resolve(copy.actor, occurred_ms=copy.occurred_ms, time_basis=copy.time_certainty)
        if match == "group":
            if copy.kind == "reaction":
                contact, basis = arvid, "reaction_echo"
            elif copy.kind.startswith("member_") or copy.kind in ("group_subject", "group_description"):
                return None, "native_identifier" if copy.provenance == "native" else "derived_claim"
            else:
                contact, basis = None, "unknown"
        elif copy.provenance != "native":
            basis = "derived_claim"
        elif match == "numeric_match":
            basis = "numeric_match"
        else:
            basis = "native_identifier"
    return (contact, basis) if contact is not None else (None, "unknown")


def _event_complete(copy: EventCopy) -> bool:
    if copy.kind == "reaction":
        return bool(copy.payload.get("removed") or copy.payload.get("emoji"))
    if copy.kind == "edit":
        return copy.payload.get("text") is not None
    return True


def _events(ex: Extracted, res: Resolution, arvid: str | None, message_ids: set[str],
            review: dict[str, Any], authors: dict[str, AuthorCorrection]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, ...], list[tuple[EventCopy, str | None, str]]] = defaultdict(list)
    purged: list[tuple[tuple[str, ...], tuple[EventCopy, str | None, str]]] = []
    for copy in ex.events:
        actor, basis = _actor(copy, res, arvid, authors.get(copy.ref))
        actor_key = actor or (copy.actor.value if copy.actor is not None else "")
        base = (copy.kind, copy.channel, copy.chat_id, copy.target_native_id or "", actor_key)
        item = (copy, actor, basis)
        if _event_complete(copy):
            payload = copy.payload
            if copy.kind in ("group_subject", "group_description") and not payload["snapshot"] and "occurredMs" in payload:
                payload = {k: v for k, v in payload.items() if k != "observedAtMs"}
            groups[(*base, canonical_json(payload))].append(item)
        else:
            purged.append((base, item))

    clusters: list[tuple[tuple[str, ...], list[tuple[EventCopy, str | None, str]]]] = []
    for key in sorted(groups):
        items = sorted(groups[key], key=lambda x: (x[0].occurred_ms is None, x[0].occurred_ms or 0,
                                                   x[0].rank, x[0].ref))
        current: list[tuple[EventCopy, str | None, str]] = []
        first_time: int | None = None
        for item in items:
            moment = item[0].occurred_ms
            if current and (moment is None or first_time is None or moment - first_time > WINDOW_MS):
                clusters.append((key, current))
                current, first_time = [], None
            current.append(item)
            if first_time is None and moment is not None:
                first_time = moment
        if current:
            clusters.append((key, current))

    unmatched: list[tuple[EventCopy, str | None, str]] = []
    for base, item in sorted(purged, key=lambda pair: _order(pair[1][0])):
        moment = item[0].occurred_ms
        unanchored_assistant_reaction = (
            item[0].kind == "reaction" and not item[0].target_native_id
            and not item[0].payload.get("emoji") and item[1] == arvid and arvid is not None
        )
        window = 10_000 if unanchored_assistant_reaction else WINDOW_MS
        candidates = [members for key, members in clusters
                      if (key[:5] == base or (unanchored_assistant_reaction
                          and key[0] == "reaction" and key[1] == base[1]
                          and key[2] == base[2] and key[4] == base[4]))
                      and moment is not None
                      and any(_event_complete(c) and (not unanchored_assistant_reaction
                              or bool(c.payload.get("emoji"))) and c.occurred_ms is not None
                              and abs(c.occurred_ms - moment) <= window for c, _, _ in members)]
        if len(candidates) == 1:
            candidates[0].append(item)
        else:
            unmatched.append(item)
    review["unmatched_event_payloads"] = sorted(item[0].ref for item in unmatched)

    rows: list[dict[str, Any]] = []
    for key, members in clusters:
        rows.append(_event_row(key, members, message_ids, authors))
    for copy, actor, basis in unmatched:
        key = (copy.kind, copy.channel, copy.chat_id, copy.target_native_id or "",
               actor or (copy.actor.value if copy.actor is not None else ""), canonical_json(copy.payload))
        rows.append(_event_row(key, [(copy, actor, basis)], message_ids, authors))
    _mark_current_reactions(rows)
    rows.sort(key=lambda r: r["event_id"])
    return rows


def _event_row(key: tuple[str, ...], members: list[tuple[EventCopy, str | None, str]],
               message_ids: set[str], authors: dict[str, AuthorCorrection]) -> dict[str, Any]:
    kind, channel, chat, target, _, payload_json = key
    copies = [c for c, _, _ in members]
    actor_item = min((item for item in members if item[0].actor_raw), default=min(members, key=lambda x: _order(x[0])),
                     key=lambda x: _order(x[0]))
    corrected = [item for item in members if item[0].ref in authors]
    actor, basis = (max(corrected, key=lambda item: (authors[item[0].ref].attestation.at_ms,
                                                   authors[item[0].ref].attestation.ref))[1:]
                    if corrected else actor_item[1:])
    occurred_ms, certainty = _best_time(copies)
    first = min(copies, key=lambda c: (c.occurred_ms is None, c.occurred_ms or 0, c.rank, c.ref))
    event_id = hashlib.sha256(canonical_json([*key, first.occurred_ms, first.ref]).encode()).hexdigest()[:32]
    target_id = f"{channel}:{chat}:{target}" if target else None
    native_event_id = next((c.native_event_id for c in sorted(copies, key=_order) if c.native_event_id), None)
    return {
        "event_id": event_id, "kind": kind, "channel": channel, "chat_id": chat,
        "target_message_id": target_id if target_id in message_ids else None,
        "target_native_id": target or None, "actor_contact_id": actor,
        "actor_identifier": actor_item[0].actor_raw, "actor_basis": basis,
        "occurred_ms": occurred_ms, "time_certainty": certainty,
        "payload": first.payload if kind in ("group_subject", "group_description") else json.loads(payload_json),
        "provenance": min((c.provenance for c in copies), key=lambda p: _PROVENANCE.get(p, 3)),
        "source_refs": sorted({r for c in copies for r in (c.ref, *c.extra_refs)}
                              | {authors[c.ref].attestation.ref for c in copies if c.ref in authors}),
        "native_event_id": native_event_id,
    }


def _mark_current_reactions(rows: list[dict[str, Any]]) -> None:
    latest: dict[tuple[Any, ...], tuple[tuple[int, str], dict[str, Any]]] = {}
    for row in rows:
        if row["kind"] != "reaction":
            continue
        row["payload"]["current"] = False
        slot = (row["channel"], row["chat_id"], row["target_native_id"],
                row["actor_contact_id"] or row["actor_identifier"])
        rank = (row["occurred_ms"] if row["occurred_ms"] is not None else -1, row["event_id"])
        if slot not in latest or rank > latest[slot][0]:
            latest[slot] = (rank, row)
    for _, row in latest.values():
        row["payload"]["current"] = not row["payload"].get("removed")


def _write(db_path: Path, res: Resolution, messages: list[dict[str, Any]], events: list[dict[str, Any]],
           files: list[tuple[str, Path]]) -> dict[str, int]:
    building = db_path.with_name(db_path.name + ".building")
    if is_protected(building):
        raise PermissionError(f"refusing to write history.db staging file into the raw archive: {building}")
    db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(building, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    owned = os.fstat(fd)
    os.close(fd)
    counts: dict[str, int] = {}
    try:
        conn = sqlite3.connect(building)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            create(conn)
            contacts = sorted(res.contacts, key=lambda c: (c.merged_into is not None, c.contact_id))
            conn.executemany("INSERT INTO contacts (contact_id, kind, role, display_name, status, merged_into, source_refs) "
                              "VALUES (?, ?, ?, ?, ?, ?, ?)", [
                (c.contact_id, c.kind, c.role, c.display_name, c.status, c.merged_into,
                 json.dumps(list(c.source_refs))) for c in contacts])
            conn.executemany(
                "INSERT INTO identifier_history (contact_id, channel, kind, value, strength, evidence,"
                " first_seen_ms, last_seen_ms, ended_ms, source_refs, valid_from_ms, valid_until_ms)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
                    (i.contact_id, i.channel, i.kind, i.value, i.strength, i.evidence, i.first_seen_ms,
                     i.last_seen_ms, i.ended_ms, json.dumps(list(i.source_refs)), i.valid_from_ms, i.valid_until_ms)
                    for i in sorted(res.identifiers, key=lambda i: (i.contact_id, i.kind, i.value))])
            conn.executemany("INSERT INTO messages (message_id, channel, chat_id, native_message_id, sender_contact_id, "
                              "sender_identifier, sender_basis, direction, sent_ms, time_certainty, text, media_json, "
                              "reply_to_native_id, mentions_json, provenance, source_refs) "
                              "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
                (m["message_id"], m["channel"], m["chat_id"], m["native_message_id"], m["sender_contact_id"],
                 m["sender_identifier"], m["sender_basis"], m["direction"], m["sent_ms"], m["time_certainty"],
                 m["text"], canonical_json(m["media"]) if m["media"] else None, m["reply_to_native_id"],
                 canonical_json(m["mentions"]) if m["mentions"] else None, m["provenance"],
                 json.dumps(m["source_refs"])) for m in messages])
            conn.executemany("INSERT INTO message_events (event_id, kind, channel, chat_id, target_message_id, "
                              "target_native_id, actor_contact_id, actor_identifier, actor_basis, occurred_ms, "
                              "time_certainty, payload_json, provenance, source_refs, native_event_id) "
                              "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
                (e["event_id"], e["kind"], e["channel"], e["chat_id"], e["target_message_id"],
                 e["target_native_id"], e["actor_contact_id"], e["actor_identifier"], e["actor_basis"],
                 e["occurred_ms"], e["time_certainty"], canonical_json(e["payload"]), e["provenance"],
                 json.dumps(e["source_refs"]), e["native_event_id"]) for e in events])
            for rel, path in files:
                data = path.read_bytes()
                counts[rel] = sum(1 for line in data.decode("utf-8", errors="replace").splitlines() if line.strip())
                conn.execute("INSERT INTO projector_state (file, lines, sha256, projector_version) VALUES (?, ?, ?, ?)",
                             (rel, counts[rel], hashlib.sha256(data).hexdigest(), PROJECTOR_VERSION))
            conn.commit()
        finally:
            conn.close()
        os.chmod(building, 0o600)
        os.replace(building, db_path)
        return counts
    finally:
        try:
            current = building.lstat()
        except FileNotFoundError:
            pass
        else:
            if (current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino):
                building.unlink()


def _report(ex: Extracted, res: Resolution, messages: list[dict[str, Any]], events: list[dict[str, Any]],
            line_counts: dict[str, int], unattached: int) -> dict[str, Any]:
    outcomes: dict[str, dict[str, int]] = defaultdict(dict)
    for (file, outcome), n in sorted(ex.outcomes.items()):
        outcomes[file][outcome] = n
    accounting = {f: {"lines": n, "accounted": sum(outcomes.get(f, {}).values())}
                  for f, n in line_counts.items()}
    live = [c for c in res.contacts if c.merged_into is None]
    return {
        "files": len(line_counts), "outcomes": dict(outcomes), "accounting": accounting,
        "accounting_ok": all(v["lines"] == v["accounted"] for v in accounting.values()),
        "contacts": len(live), "provisional_contacts": sum(c.status == "provisional" for c in live),
        "merged_contacts": len(res.contacts) - len(live), "messages": len(messages),
        "events": len(events), "unattached_media": unattached, "review": res.review,
    }
