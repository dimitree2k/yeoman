"""Durable generated-ID reservations are aliases, never new ownership evidence."""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from yeoman_shared.raw_archive.records import (
    CommitCallback,
    CommittedLine,
    _validate_contact_id_record,
    append_contact_id_records,
)

from .ids import classify
from .layer1 import Layer1Line
from .resolve import ContactRow, GeneratedContactId, Resolution


def apply_lineage(resolution: Resolution, records: Sequence[Layer1Line]) -> None:
    rows = {row.contact_id: row for row in resolution.contacts}
    generated = {item.contact_id for item in resolution.generated_ids}
    review = resolution.review.setdefault('contact_id_lineage', [])
    seen: dict[str, tuple[object, ...]] = {}
    for line in records:
        record = line.record or {}
        _validate_contact_id_record(record)
        cid = record['contact_id']
        signature = tuple(record[key] for key in ('seed', 'value', 'valid_from_ms', 'valid_until_ms'))
        if cid in seen:
            if seen[cid] != signature:
                raise ValueError('conflicting contact ID lineage reservation')
            continue
        seen[cid] = signature
        ident = classify(record['value'])
        if ident and ident.kind == 'numeric':
            ident = resolution.canonical.get(ident.value, ident)
        start = record['valid_from_ms'] if record['valid_from_ms'] is not None else -float('inf')
        end = record['valid_until_ms'] if record['valid_until_ms'] is not None else float('inf')
        intervals = []
        for row in resolution._identifier_index.get((ident.kind, ident.value), ()) if ident else ():
            lo = max(start, row.valid_from_ms if row.valid_from_ms is not None else -float('inf'))
            hi = min(end, row.valid_until_ms if row.valid_until_ms is not None else float('inf'))
            if lo < hi:
                intervals.append((lo, hi, resolution.terminal(row.contact_id)))
        owners = {owner for _, _, owner in intervals}
        covered = start
        for lo, hi, _ in sorted(intervals):
            if lo > covered:
                break
            covered = max(covered, hi)
        reason = 'absent' if not owners else 'multiple' if len(owners) > 1 else 'gap' if covered < end else None
        existing = rows.get(cid)
        target = next(iter(owners)) if len(owners) == 1 else None
        if existing is not None and cid not in generated and (reason or resolution.terminal(cid) != target):
            review.append({'contact_id': cid, 'reason': 'collision', 'ref': line.ref})
            continue
        if reason:
            # Retain source-derived fix-round-2 rows; lineage adds no alias across a gap.
            review.append({'contact_id': cid, 'reason': reason, 'owners': sorted(owners), 'ref': line.ref})
            continue
        assert target is not None
        if existing is None:
            owner = rows[target]
            rows[cid] = ContactRow(cid, owner.kind, None, None, owner.status, target,
                                   tuple(record['source_refs']))
    resolution.contacts = sorted(rows.values(), key=lambda row: row.contact_id)
    resolution.generated_ids = tuple(item for item in resolution.generated_ids if item.contact_id in rows)
    for contact in resolution.contacts:
        resolution.terminal(contact.contact_id)


def publish_lineage(raw_root: Path, generated: Sequence[GeneratedContactId], *,
                    first_published_ms: int, on_committed: CommitCallback | None = None) -> tuple[CommittedLine, ...]:
    batch = []
    for item in generated:
        record = {'raw_archive_version': 1, 'kind': 'contact_id', 'channel': 'whatsapp',
                  'contact_id': item.contact_id, 'seed': item.seed, 'value': item.value,
                  'valid_from_ms': item.valid_from_ms, 'valid_until_ms': item.valid_until_ms,
                  'source_refs': sorted(set(item.source_refs)), 'first_published_ms': first_published_ms}
        batch.append(record)
    receipts = append_contact_id_records(raw_root, batch, on_committed=on_committed)
    if any(receipt is None for receipt in receipts):
        raise ValueError('contact ID lineage disposed before publication')
    return tuple(receipt for receipt in receipts if receipt is not None)
