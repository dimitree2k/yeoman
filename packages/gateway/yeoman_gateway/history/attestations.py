"""Owner decisions stored in Layer 1 so every history rebuild keeps them."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yeoman_shared.raw_archive.paths import ProtectedPathError, is_protected

from .ids import classify
from .layer1 import Layer1Line, canonical_json, write_jsonl_once

ATTESTATION_VERSION = 1
SEED_AT_MS = 1_791_158_400_000
REQUIRED: dict[str, tuple[str, ...]] = {
    "contact": ("identifiers",),
    "identifier": ("anchor", "identifier"),
    "merge": ("a", "b"),
    "unmerge": ("a", "b"),
    "name": ("anchor", "name"),
    "message_author": ("message_id", "anchor"),
    "identifier_ended": ("identifier", "ended_ms"),
}
_IDENTIFIER_FIELDS = ("anchor", "identifier", "a", "b")
_ENVELOPE = frozenset({"attestation_version", "type", "at_ms", "by", "note"})


@dataclass(frozen=True)
class Attestation:
    type: str
    at_ms: int
    ref: str
    fields: dict[str, Any]


def _is_strong(value: Any) -> bool:
    ident = classify(value)
    return ident is not None and ident.strong


def _check(record: Any) -> None:
    if not isinstance(record, dict):
        raise ValueError("attestation must be a JSON object")
    type_ = record.get("type")
    if not isinstance(type_, str):
        raise ValueError("attestation type must be a string")
    if type_ not in REQUIRED:
        raise ValueError(f"unknown attestation type: {type_!r}")
    if not isinstance(record.get("at_ms"), int) or isinstance(record["at_ms"], bool):
        raise ValueError("at_ms must be an integer")
    for name in REQUIRED[type_]:
        if record.get(name) in (None, "", []):
            raise ValueError(f"{type_} needs {name}")
    for name in _IDENTIFIER_FIELDS:
        if name in record and not _is_strong(record[name]):
            raise ValueError(f"{name} must be a full WhatsApp identifier: {record[name]!r}")
    if type_ == "contact":
        identifiers = record["identifiers"]
        if not isinstance(identifiers, list) or not identifiers or not all(
            _is_strong(value) for value in identifiers
        ):
            raise ValueError("contact identifiers must be full WhatsApp identifiers")
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
