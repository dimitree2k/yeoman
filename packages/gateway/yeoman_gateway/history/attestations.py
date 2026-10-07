"""Owner decisions stored in Layer 1 so every history rebuild keeps them."""

from __future__ import annotations

import hashlib
import os
import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from yeoman_shared.raw_archive.paths import ProtectedPathError, is_protected

from .ids import classify
from .layer1 import Layer1Line, canonical_json, write_jsonl_once

if TYPE_CHECKING:
    from .extract import EventCopy, MessageCopy

ATTESTATION_VERSION = 2
SEED_AT_MS = 1_791_158_400_000
REQUIRED: dict[str, tuple[str, ...]] = {
    "contact": ("identifiers",),
    "identifier": ("anchor", "identifier"),
    "merge": ("a", "b"),
    "unmerge": ("a", "b"),
    "name": ("anchor", "name"),
    "message_author": ("message_id", "anchor"),
    "author": ("source_ref", "anchor"),
    "identifier_ended": ("identifier", "ended_ms"),
}
_IDENTIFIER_FIELDS = ("anchor", "identifier", "a", "b")
_ENVELOPE = frozenset({"attestation_version", "type", "at_ms", "by", "note"})
_WHATSAPP_ID = re.compile(
    r"[0-9]+(?::[0-9]+)?@(lid|s\.whatsapp\.net|c\.us|newsletter)", re.IGNORECASE
)
_SOURCE_REF = re.compile(
    r"(?:whatsapp|backfill|derived|owner)/[^/\\#\s]+\.jsonl#[1-9][0-9]*(?:/(?:0|[1-9][0-9]*))?"
)


@dataclass(frozen=True)
class Attestation:
    type: str
    at_ms: int
    ref: str
    fields: dict[str, Any]


def _is_strong(value: Any) -> bool:
    if not isinstance(value, str) or value != value.strip():
        return False
    ident = classify(value)
    return ident is not None and ident.strong and _WHATSAPP_ID.fullmatch(value) is not None


def _is_stored_identifier(value: Any) -> bool:
    return _is_strong(value) or (
        isinstance(value, str) and value == value.strip()
        and (ident := classify(value)) is not None and ident.kind == "telegram"
    )


def _check(record: Any) -> None:
    if not isinstance(record, dict):
        raise ValueError("attestation must be a JSON object")
    type_ = record.get("type")
    if not isinstance(type_, str):
        raise ValueError("attestation type must be a string")
    if type_ not in REQUIRED:
        raise ValueError(f"unknown attestation type: {type_!r}")
    version = record.get("attestation_version", 1)
    if type(version) is not int or version not in (1, ATTESTATION_VERSION):
        raise ValueError("unsupported attestation_version")
    if not isinstance(record.get("at_ms"), int) or isinstance(record["at_ms"], bool):
        raise ValueError("at_ms must be an integer")
    if version == ATTESTATION_VERSION or type_ == "author":
        if not isinstance(record.get("note"), str) or not record["note"].strip():
            raise ValueError("note must be a nonempty string")
    for name in REQUIRED[type_]:
        if record.get(name) in (None, "", []):
            raise ValueError(f"{type_} needs {name}")
    for name in _IDENTIFIER_FIELDS:
        check = _is_stored_identifier if name == "identifier" else _is_strong
        if name in record and not check(record[name]):
            raise ValueError(f"{name} must be a supported full identifier: {record[name]!r}")
    if type_ == "author":
        source_ref = record["source_ref"]
        if not isinstance(source_ref, str) or _SOURCE_REF.fullmatch(source_ref) is None:
            raise ValueError("source_ref must be a relative Layer 1 file#positive-line[/segment] ref")
        anchor = classify(record["anchor"])
        if anchor is None or anchor.kind not in ("lid", "pn_jid"):
            raise ValueError("author anchor must be a person WhatsApp identifier")
    if type_ == "identifier":
        start, end = record.get("valid_from_ms"), record.get("valid_until_ms")
        for name in ("valid_from_ms", "valid_until_ms"):
            if record.get(name) is not None and type(record[name]) is not int:
                raise ValueError(f"{name} must be an integer or null")
        if start is not None and end is not None and start >= end:
            raise ValueError("valid_from_ms must be less than valid_until_ms")
    if type_ == "contact":
        identifiers = record["identifiers"]
        if not isinstance(identifiers, list) or not identifiers or not all(
            _is_stored_identifier(value) for value in identifiers
        ):
            raise ValueError("contact identifiers must be supported full identifiers")
        if record.get("role") not in (None, "owner", "assistant"):
            raise ValueError("role must be owner, assistant or absent")
    if type_ == "identifier_ended" and (
        not isinstance(record["ended_ms"], int) or isinstance(record["ended_ms"], bool)
    ):
        raise ValueError("ended_ms must be an integer")


def make(type_: str, at_ms: int, note: str, **fields: Any) -> dict[str, Any]:
    record = {
        "attestation_version": ATTESTATION_VERSION,
        "type": type_,
        "at_ms": at_ms,
        "by": "owner",
        "note": note,
        **fields,
    }
    _check(record)
    return record


def parse(line: Layer1Line) -> Attestation:
    if line.record is None:
        raise ValueError(f"{line.ref}: not a JSON object")
    _check(line.record)
    fields = {key: value for key, value in line.record.items() if key not in _ENVELOPE}
    return Attestation(line.record["type"], line.record["at_ms"], line.ref, fields)


def seed_records() -> list[dict[str, Any]]:
    return [
        make(
            "contact",
            SEED_AT_MS,
            "Arvid, the assistant; the bridge runs on this number",
            identifiers=["4915202777685@s.whatsapp.net"],
            name="Arvid Falkenrath",
            role="assistant",
        ),
        make(
            "contact",
            SEED_AT_MS,
            "Owner",
            identifiers=["491757070305@s.whatsapp.net"],
            name="Dimi",
            role="owner",
        ),
        make(
            "contact",
            SEED_AT_MS,
            "Manual entry by the owner",
            identifiers=["4915140189391@s.whatsapp.net"],
            name="Matthias Hoffmann",
        ),
    ]


def write_seed(raw_root: Path) -> Path:
    path = raw_root / "owner" / "attestations.jsonl"
    write_jsonl_once(path, seed_records())
    return path


def append(path: Path, record: dict[str, Any]) -> None:
    _check(record)
    if is_protected(path):
        raise ProtectedPathError(f"refusing write to protected raw archive path: {path}")
    with path.open("a", encoding="utf-8") as out:
        out.write(canonical_json(record) + "\n")
        out.flush()
        os.fsync(out.fileno())


def message_copy_id(copy: MessageCopy) -> str:
    """Use the same entity identity for legacy author targets and projection."""
    prefix = f"{copy.channel}:{copy.chat_id}:"
    if copy.native_id:
        return prefix + copy.native_id
    identity = copy.batch_key or copy.ref
    return prefix + "derived:" + hashlib.sha256(identity.encode()).hexdigest()[:32]


def _claim(att: Attestation, source_ref: str) -> dict[str, Any]:
    return {"source_ref": source_ref, "attestation_ref": att.ref,
            "anchor": att.fields["anchor"], "at_ms": att.at_ms}


def resolve_author_targets(
    attestations: Sequence[Attestation], messages: Sequence[MessageCopy], events: Sequence[EventCopy],
) -> tuple[dict[str, Attestation], list[dict[str, Any]]]:
    """Select per-copy corrections without altering the preserved source evidence."""
    refs: dict[str, set[str]] = defaultdict(set)
    bases: dict[str, list[MessageCopy]] = defaultdict(list)
    legacy: dict[str, set[str]] = defaultdict(set)
    entities: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for copy in messages:
        for ref in (copy.ref, *copy.extra_refs):
            refs[ref].add(copy.ref)
        legacy[message_copy_id(copy)].add(copy.ref)
        if copy.parent_native_id is not None or copy.segmented:
            bases[copy.ref.rsplit("/", 1)[0]].append(copy)
        if copy.native_id:
            entities[("message", copy.channel, copy.chat_id, copy.native_id)].append(copy.ref)
    event_evidence: dict[str, dict[str, Any]] = {}
    for event in events:
        for ref in (event.ref, *event.extra_refs):
            refs[ref].add(event.ref)
        if event.native_event_id:
            entities[(event.kind, event.channel, event.chat_id, event.native_event_id)].append(event.ref)
            event_evidence[event.ref] = {"target_native_id": event.target_native_id,
                                         "payload": event.payload}
    claims: dict[str, list[Attestation]] = defaultdict(list)
    review: list[dict[str, Any]] = []
    for att in sorted(attestations, key=lambda a: (a.at_ms, a.ref)):
        if att.type not in ("author", "message_author"):
            continue
        target = att.fields.get("source_ref" if att.type == "author" else "message_id")
        if not isinstance(target, str):
            review.append({"reason": "invalid_target", "attestation_ref": att.ref, "target": target})
            continue
        selected = refs.get(target, set()) if att.type == "author" else legacy.get(target, set())
        if att.type == "author" and target in bases:
            selected = {c.ref for c in bases[target]
                        if c.parent_native_id and c.native_id == c.parent_native_id}
        if not selected or (att.type == "author" and len(selected) != 1):
            review.append({"reason": "ambiguous_target" if selected else "missing_or_non_content_target",
                           "attestation_ref": att.ref, "target": target,
                           "selected_refs": sorted(selected)})
            continue
        anchor = classify(att.fields.get("anchor"))
        if anchor is None or anchor.kind not in ("lid", "pn_jid"):
            review.append({"reason": "invalid_author_anchor", "attestation_ref": att.ref,
                           "target": target})
            continue
        for ref in selected:
            claims[ref].append(att)
    winners = {ref: max(items, key=lambda a: (a.at_ms, a.ref)) for ref, items in sorted(claims.items())}
    for ref, items in sorted(claims.items()):
        if len({a.fields["anchor"] for a in items}) > 1:
            review.append({"reason": "conflicting_author_claims", "source_ref": ref,
                           "winner_ref": winners[ref].ref, "claims": [_claim(a, ref) for a in items]})
    for entity, source_refs in sorted(entities.items()):
        corrected = [(ref, winners[ref]) for ref in sorted(source_refs) if ref in winners]
        if len({a.fields["anchor"] for _, a in corrected}) > 1:
            review.append({"reason": "conflicting_entity_authors", "entity": list(entity),
                           "claims": [{**_claim(a, ref), **event_evidence.get(ref, {})}
                                      for ref, a in corrected]})
    return winners, review


def validate_owner_package(raw_root: Path, records: Sequence[dict[str, Any]]) -> None:
    """Validate the whole local package against current native/identity evidence, read-only."""
    from yeoman_shared.raw_archive.records import (
        _owner_is_disposed,
        dumps,
        preflight_owner_paths,
        validate_owner_envelope,
    )

    from .extract import extract
    from .layer1 import iter_layer1, layer1_files
    from .resolve import resolve

    preflight_owner_paths(raw_root)
    for record in records:
        _check(record)
        validate_owner_envelope(record)
    paths = [raw_root, *(path for _, path in layer1_files([raw_root]))]
    if any(parent.is_symlink() for path in paths for parent in (path, *path.parents)):
        raise ValueError('owner evidence must not traverse symlinks')
    existing = list(iter_layer1([raw_root]))
    owner_path = raw_root / 'owner/attestations.jsonl'
    owner_bytes = owner_path.read_bytes() if owner_path.exists() else b''
    count = owner_bytes.count(b'\n') + int(bool(owner_bytes) and not owner_bytes.endswith(b'\n'))
    proposed = [parse(Layer1Line(f'owner/attestations.jsonl#{count + index}', record))
                for index, record in enumerate(records, 1)]
    evidence = extract(existing)
    # Author/name/merge fields cannot invent an otherwise unknown anchor.
    initial = list(evidence.identity.attestations)
    evidence.identity.attestations = initial + [a for a in proposed if a.type == 'contact']
    anchors = resolve(evidence.identity)
    for att in proposed:
        if att.type == 'identifier' and anchors.contact_for_anchor(att.fields['anchor']) is None:
            raise ValueError('unknown or ambiguous identifier anchor')
    evidence.identity.attestations += [a for a in proposed if a.type in ('identifier', 'identifier_ended')]
    resolved = resolve(evidence.identity)
    for att in proposed:
        fields = ('a', 'b') if att.type in ('merge', 'unmerge') else ('anchor',)
        for field in fields:
            if field in att.fields and resolved.contact_for_anchor(att.fields[field]) is None:
                raise ValueError('unknown or ambiguous owner anchor')
    refs = {a.ref for a in proposed}
    for entries in resolved.review.values():
        for entry in entries:
            if any(ref in canonical_json(entry) for ref in refs):
                raise ValueError('ambiguous owner identifier decision')
    _, review = resolve_author_targets(proposed, evidence.messages, evidence.events)
    if review:
        raise ValueError('invalid, ambiguous or conflicting author targets')
    # A legacy native-ID locator cannot silently select colliding original rows.
    for att in proposed:
        if att.type == 'message_author':
            selected = [c for c in evidence.messages if message_copy_id(c) == att.fields['message_id']]
            if len({(c.text, c.batch_key) for c in selected}) > 1:
                raise ValueError('legacy author locator selects colliding content')

    # Prove every publisher source binding before the first package write; confirm repeats under lock.
    for record in records:
        _owner_is_disposed(raw_root, record, dumps(record))
