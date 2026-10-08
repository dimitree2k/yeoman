"""Disposable dependency cache and atomic committed-prefix projection."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from bisect import bisect_left, bisect_right, insort
from collections import Counter, defaultdict
from collections.abc import Sequence
from copy import copy, deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from yeoman_shared.raw_archive.records import (
    PURGE_DISPOSITION_LOCK,
    SourceBoundary,
    _no_symlinks,
    lock_file,
)

from .extract import Description, EventCopy, Extracted, MediaRecord, MessageCopy, extract
from .ids import Ident, classify, numeric_part
from .layer1 import SUBDIRS, Layer1Line, canonical_json
from .project import (
    WINDOW_MS,
    ProjectionRows,
    _authors,
    _direction,
    _events,
    _messages,
    build_rows,
    write_rows,
)
from .resolve import MAX_REFS, IdentityInput, Resolution, Sighting, resolve
from .schema import PROJECTOR_VERSION, SCHEMA_VERSION


class RebuildRequired(ValueError):  # noqa: N818 - exact Task 2/3 contract name
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass
class ProjectionDelta:
    rows: ProjectionRows
    message_ids: set[str]
    event_ids: set[str]
    requires_rebuild: bool
    reason: str | None
    pending_pairs: dict[str, list[str]]


def _read_prefix(raw_root: Path, boundaries: Sequence[SourceBoundary]) -> tuple[
    list[Layer1Line], dict[str, int], dict[str, tuple[int, int]], dict[str, bytes]
]:
    lines, blanks, inodes, data_by_file = [], {}, {}, {}
    for boundary in boundaries:
        rel = boundary.relative_path
        parts = Path(rel).parts
        if (len(parts) != 2 or parts[0] not in SUBDIRS or not parts[1].endswith('.jsonl')
                or rel != '/'.join(parts) or rel in data_by_file
                or boundary.end_offset < 0 or boundary.line_number < 0):
            raise RebuildRequired('invalid committed boundary')
        path = raw_root / rel
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise RebuildRequired('source traverses symlink')
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as source:
                source_stat = os.fstat(source.fileno())
                if not stat.S_ISREG(source_stat.st_mode):
                    raise RebuildRequired('source is not a regular file')
                data = source.read(boundary.end_offset)
        except OSError as exc:
            raise RebuildRequired('checkpointed source unavailable') from exc
        if (len(data) != boundary.end_offset or data.count(b'\n') != boundary.line_number
                or (data and not data.endswith(b'\n'))
                or hashlib.sha256(data).hexdigest() != boundary.prefix_sha256):
            raise RebuildRequired('committed prefix changed')
        inodes[rel] = (source_stat.st_dev, source_stat.st_ino)
        data_by_file[rel] = data
        blanks[rel] = 0
        for number, physical in enumerate(data.split(b'\n')[:-1], 1):
            text = physical.decode('utf-8', errors='replace')
            if not text.strip():
                blanks[rel] += 1
                continue
            try:
                record = json.loads(text)
            except json.JSONDecodeError:
                record = None
            lines.append(Layer1Line(f'{rel}#{number}', record if isinstance(record, dict) else None))
    return sorted(lines, key=lambda line: (line.ref.split('#')[0], int(line.ref.split('#')[1]))), blanks, inodes, data_by_file


def _pair_key(line: Layer1Line) -> str | None:
    record = line.record or {}
    if (line.ref.startswith('whatsapp/') and record.get('channel', 'whatsapp') == 'whatsapp'
            and record.get('kind') in ('outbound_request', 'outbound_result')
            and record.get('correlation_id')):
        return canonical_json([str(record.get('account') or 'default'), str(record['correlation_id'])])
    return None


def _units(lines: Sequence[Layer1Line]) -> dict[str, list[Layer1Line]]:
    groups: dict[str, list[Layer1Line]] = {}
    for line in lines:
        key = 'pair:' + pair if (pair := _pair_key(line)) else line.ref
        groups.setdefault(key, []).append(line)
    return groups


def _combine(units: dict[str, Extracted]) -> Extracted:
    out = Extracted()
    for key in sorted(units, key=lambda key: (key.startswith('pair:'),
                      key if key.startswith('pair:') else key.split('#')[0],
                      0 if key.startswith('pair:') else int(key.split('#')[1]))):
        ex = units[key]
        for name in ('messages', 'events', 'descriptions', 'media_records', 'attestations'):
            getattr(out, name).extend(getattr(ex, name))
        out.pending_pairs.update(ex.pending_pairs)
        out.outcomes.update(ex.outcomes)
        for name, entries in ex.review.items():
            out.review.setdefault(name, []).extend(entries)
        inp = ex.identity
        for ident, sighting in inp.sightings.items():
            if ident in out.identity.sightings:
                out.identity.sightings[ident].merge(sighting)
            else:
                out.identity.sightings[ident] = deepcopy(sighting)
        out.identity.groups.update(inp.groups)
        out.identity.links.extend(inp.links)
        out.identity.link_times.update(inp.link_times)
        for node, names in inp.names.items():
            out.identity.names[node].extend(names)
        for ref, contact in inp.contact_records.items():
            out.identity.contact_records.setdefault(ref, contact)
    out.identity.attestations = list(out.attestations)
    return out


def _chats(ex: Extracted) -> set[tuple[str, str]]:
    copies: list[MessageCopy | EventCopy | Description | MediaRecord] = [
        *ex.messages, *ex.events, *ex.descriptions, *ex.media_records]
    return {(copy.channel, copy.chat_id) for copy in copies}


def _identity_change(old: Resolution, new: Resolution, ex: Extracted) -> bool:
    # Metadata can change locally; ownership and numeric applicability cannot.
    if (not {c.contact_id for c in old.contacts} <= {c.contact_id for c in new.contacts}
            or old.role_contact != new.role_contact
            or any(new.canonical.get(key) != value for key, value in old.canonical.items())):
        return True
    old_values = {(row.kind, row.value) for row in old.identifiers if row.kind != 'push_name'}
    def ownership(res: Resolution) -> set[tuple[Any, ...]]:
        return {(row.contact_id, row.kind, row.value, row.valid_from_ms, row.valid_until_ms, row.ended_ms)
                for row in res.identifiers if (row.kind, row.value) in old_values}
    if ownership(old) != ownership(new):
        return True
    for row in old.identifiers:
        if row.kind == 'push_name':
            continue
        ident = classify(row.value)
        for ms, basis in ((None, 'unknown'), (row.first_seen_ms, 'provider_timestamp'),
                          (row.last_seen_ms, 'provider_timestamp')):
            if old.resolve(ident, occurred_ms=ms, time_basis=basis) != new.resolve(ident, occurred_ms=ms, time_basis=basis):
                return True
    copies: list[MessageCopy | EventCopy] = [*ex.messages, *ex.events]
    for entity in copies:
        ident = entity.sender if isinstance(entity, MessageCopy) else entity.actor
        if old.resolve(ident, occurred_ms=entity.occurred_ms, time_basis=entity.time_certainty) != new.resolve(
                ident, occurred_ms=entity.occurred_ms, time_basis=entity.time_certainty):
            return True
    return False



@dataclass
class _File:
    boundary: SourceBoundary
    digest: Any
    inode: tuple[int, int]
    size: int
    mtime: int


def _check_file(state: _File | None, info: os.stat_result) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise RebuildRequired('source is not a regular file')
    if state and ((info.st_dev, info.st_ino) != state.inode or info.st_size < state.boundary.end_offset
                  or (info.st_size == state.size and info.st_mtime_ns != state.mtime)):
        raise RebuildRequired('checkpointed source changed')


def _tail(raw_root: Path, boundary: SourceBoundary, old: _File | None) -> tuple[list[Layer1Line], int, _File]:
    rel = boundary.relative_path
    parts = Path(rel).parts
    if (len(parts) != 2 or parts[0] not in SUBDIRS or not parts[1].endswith('.jsonl')
            or rel != '/'.join(parts) or boundary.end_offset < 0 or boundary.line_number < 0):
        raise RebuildRequired('invalid committed boundary')
    path = raw_root / rel
    _no_symlinks(path)
    try:
        fd = lock_file(path)
        try:
            info = os.fstat(fd)
            _check_file(old, info)
            offset = old.boundary.end_offset if old else 0
            number = old.boundary.line_number if old else 0
            if boundary.end_offset < offset or info.st_size < boundary.end_offset:
                raise RebuildRequired('target omits or precedes a checkpoint')
            data = os.pread(fd, boundary.end_offset - offset, offset)
        finally:
            os.close(fd)
    except OSError as exc:
        raise RebuildRequired('checkpointed source unavailable') from exc
    digest = old.digest.copy() if old else hashlib.sha256()
    digest.update(data)
    if (len(data) != boundary.end_offset - offset or (data and not data.endswith(b'\n'))
            or number + data.count(b'\n') != boundary.line_number or digest.hexdigest() != boundary.prefix_sha256):
        raise RebuildRequired('committed prefix changed')
    lines, blanks = [], 0
    for number, physical in enumerate(data.split(b'\n')[:-1], number + 1):
        text = physical.decode('utf-8', errors='replace')
        if not text.strip():
            blanks += 1
            continue
        try:
            record = json.loads(text)
        except json.JSONDecodeError:
            record = None
        lines.append(Layer1Line(f'{rel}#{number}', record if isinstance(record, dict) else None))
    return lines, blanks, _File(boundary, digest, (info.st_dev, info.st_ino), info.st_size, info.st_mtime_ns)


def _identity_evidence(ex: Extracted) -> bool:
    return bool(ex.identity.sightings or ex.identity.groups or ex.identity.links or ex.identity.names
                or ex.identity.contact_records or ex.attestations)


def _msg_key(c: MessageCopy) -> tuple[str, str, str]:
    return c.channel, c.chat_id, c.native_id or ('batch:' + c.batch_key if c.batch_key else 'ref:' + c.ref)


def _target_key(c: Any) -> tuple[str, str, str]:
    return c.channel, c.chat_id, c.target_native_id or ''


def _ref_order(ref: str) -> tuple[str, int]:
    path, number = ref.split('#', 1)
    return path, int(number.split('/')[0])


@dataclass
class _Plan:
    delta: ProjectionDelta
    groups: dict[str, list[Layer1Line]]
    units: dict[str, Extracted]
    identity: IdentityInput | None
    sightings: dict[Ident, Sighting]
    replacements: dict[int, Any]
    contact_replacements: dict[int, Any]


class ProjectionIndex:
    _blanks: dict[str, int]
    _boundaries: tuple[SourceBoundary, ...]
    _files: dict[str, _File]
    _groups: dict[str, list[Layer1Line]]
    _extracted: dict[str, Extracted]
    _input: IdentityInput
    _resolution: Resolution
    _report: dict[str, Any]
    _pending: dict[str, list[str]]
    _outcomes: dict[str, dict[str, int]]
    _messages_rows: dict[str, dict[str, Any]]
    _events_rows: dict[str, dict[str, Any]]
    _by_key: dict[tuple[str, str, str], set[str]]
    _by_target: dict[tuple[str, str, str], set[str]]
    _by_chat: dict[tuple[str, str], set[str]]
    _times: dict[tuple[Any, ...], list[tuple[int, str, str]]]
    _reaction_times: dict[tuple[str, str], list[tuple[int, str, str]]]
    _loose_texts: dict[tuple[str, str, str], set[str | None]]
    _unit_rows: dict[str, set[str]]
    _unit_events: dict[str, set[str]]
    _ref_unit: dict[str, str]
    _copies_by_ident: dict[str, set[str]]
    _chat_names: dict[tuple[str, str], Counter[str]]
    _author_attestations: list[Any]
    _author_refs: dict[str, list[Any]]
    _unresolved_authors: set[str]
    _staged: _Plan | None

    @classmethod
    def from_prefix(cls, raw_root: Path, boundaries: Sequence[SourceBoundary]) -> ProjectionIndex:
        self = cls()
        lines, self._blanks, inodes, data = _read_prefix(raw_root, boundaries)
        self._boundaries = tuple(sorted(boundaries, key=lambda b: b.relative_path))
        self._files = {}
        for boundary in self._boundaries:
            info = (raw_root / boundary.relative_path).stat()
            if (info.st_dev, info.st_ino) != inodes[boundary.relative_path] or info.st_size < boundary.end_offset:
                raise RebuildRequired('source changed during startup validation')
            self._files[boundary.relative_path] = _File(boundary, hashlib.sha256(data[boundary.relative_path]),
                                                       (info.st_dev, info.st_ino), info.st_size, info.st_mtime_ns)
        self._groups = _units(lines)
        self._extracted = {key: extract(group) for key, group in self._groups.items()}
        combined = _combine(self._extracted)
        rows = build_rows(combined)
        self._input = combined.identity
        self._resolution = rows.resolution
        self._report = rows.report
        self._pending = combined.pending_pairs
        self._outcomes = {file: dict(counts) for file, counts in rows.report['outcomes'].items()}
        self._messages_rows = {m['message_id']: m for m in rows.messages}
        self._events_rows = {e['event_id']: e for e in rows.events}
        self._by_key = defaultdict(set)
        self._by_target = defaultdict(set)
        self._by_chat = defaultdict(set)
        self._times = defaultdict(list)
        self._reaction_times = defaultdict(list)
        self._loose_texts = defaultdict(set)
        self._unit_rows = defaultdict(set)
        self._unit_events = defaultdict(set)
        self._ref_unit = {}
        self._copies_by_ident = defaultdict(set)
        self._chat_names = defaultdict(Counter)
        self._author_attestations = [a for a in combined.attestations if a.type in ('author', 'message_author')]
        self._author_refs = defaultdict(list)
        for att in self._author_attestations:
            self._author_refs[str(att.fields.get('source_ref', att.fields.get('message_id')))].append(att)
        self._unresolved_authors = {item['attestation_ref'] for item in rows.report['review'].get('author_targets', []) if 'attestation_ref' in item}
        for rel, count in self._blanks.items():
            if count:
                self._outcomes.setdefault(rel, {})['skipped:blank'] = count
        self._staged = None
        for key, ex in self._extracted.items():
            self._index_unit(key, ex, True)
        for row in rows.messages:
            self._index_row(row, False, True)
        for row in rows.events:
            self._index_row(row, True, True)
        self._identity_indexes()
        return self

    def _identity_indexes(self) -> None:
        self._contact_positions = {row.contact_id: i for i, row in enumerate(self._resolution.contacts)}
        self._known_links = {tuple(link[:3]) for link in self._input.links}
        self._known_names = {node: {name for _, name, _ in names} for node, names in self._input.names.items()}
        self._fixed_names = {ref for ref, record in self._input.contact_records.items() if record.preferred_name or record.display_name}
        for att in self._input.attestations:
            if att.type == 'name' or (att.type == 'contact' and att.fields.get('name')):
                value = att.fields.get('anchor') or next(iter(att.fields.get('identifiers', [])), '')
                cid = self._resolution.resolve(classify(value))[0]
                if cid:
                    self._fixed_names.add(cid)
        self._ident_positions = {id(row): i for i, row in enumerate(self._resolution.identifiers)}
        self._idents_by_contact = defaultdict(list)
        self._numeric_values = defaultdict(set)
        for row in self._resolution.identifiers:
            self._idents_by_contact[row.contact_id].append(row)
            if row.kind != 'push_name':
                self._numeric_values[numeric_part(row.value)].add(row.value)

    def _index_unit(self, key: str, ex: Extracted, add: bool) -> None:
        def update(index: dict[Any, set[str]], slot: Any) -> None:
            if add:
                index[slot].add(key)
            else:
                index[slot].discard(key)
        for line in self._groups.get(key, []):
            if add:
                self._ref_unit[line.ref] = key
        for name in ('messages', 'events', 'descriptions', 'media_records'):
            for c in getattr(ex, name):
                update(self._by_chat, (c.channel, c.chat_id))
        for c in ex.messages:
            update(self._by_key, _msg_key(c))
            if c.sender:
                update(self._copies_by_ident, c.sender.value)
            if c.occurred_ms is not None and not c.batch_key:
                slot = (c.channel, c.chat_id, _direction([c]), c.text, bool(c.native_id))
                item = (c.occurred_ms, c.ref, key)
                if add:
                    insort(self._times[slot], item)
                else:
                    self._times[slot].remove(item)
                if not c.native_id:
                    self._loose_texts[slot[:3]].add(c.text)
        for c in ex.events:
            update(self._by_target, _target_key(c))
            if c.kind == 'reaction' and c.occurred_ms is not None:
                items = self._reaction_times[(c.channel, c.chat_id)]
                item = c.occurred_ms, c.ref, key
                if add:
                    insort(items, item)
                else:
                    items.remove(item)
            if c.actor:
                update(self._copies_by_ident, c.actor.value)
        attachments: list[Description | MediaRecord] = [*ex.descriptions, *ex.media_records]
        for attachment in attachments:
            update(self._by_key, (attachment.channel, attachment.chat_id, attachment.native_id))

    def _index_row(self, row: dict[str, Any], event: bool, add: bool) -> None:
        index = self._unit_events if event else self._unit_rows
        ident = row['event_id' if event else 'message_id']
        if not event and row.get('_sender_name') and row['sender_contact_id'] and row['sender_basis'] != 'push_name':
            names = self._chat_names[(row['chat_id'], row['_sender_name'])]
            names[row['sender_contact_id']] += 1 if add else -1
            if not names[row['sender_contact_id']]:
                del names[row['sender_contact_id']]
        for ref in row['source_refs']:
            physical = ref.split('#')[0] + '#' + ref.split('#')[1].split('/')[0]
            unit = self._ref_unit.get(physical)
            if unit is not None:
                if add:
                    index[unit].add(ident)
                else:
                    index[unit].discard(ident)

    def target(self, raw_root: Path) -> tuple[SourceBoundary, ...]:
        if not raw_root.is_dir():
            raise RebuildRequired('raw root unavailable')
        _no_symlinks(raw_root / PURGE_DISPOSITION_LOCK)
        root_fd = lock_file(raw_root / PURGE_DISPOSITION_LOCK, create=True)
        found: dict[str, SourceBoundary] = {}
        try:
            for sub in SUBDIRS:
                _no_symlinks(raw_root / sub)
                for path in sorted((raw_root / sub).glob('*.jsonl')):
                    _no_symlinks(path)
                    rel = path.relative_to(raw_root).as_posix()
                    old = self._files.get(rel)
                    fd = lock_file(path)
                    try:
                        info = os.fstat(fd)
                        _check_file(old, info)
                        offset = old.boundary.end_offset if old else 0
                        data = os.pread(fd, info.st_size - offset, offset)
                        data = data[:data.rfind(b'\n') + 1]
                        digest = old.digest.copy() if old else hashlib.sha256()
                        digest.update(data)
                        found[rel] = SourceBoundary(rel, (old.boundary.line_number if old else 0) + data.count(b'\n'),
                                                    offset + len(data), digest.hexdigest())
                    finally:
                        os.close(fd)
            if not self._files.keys() <= found.keys():
                raise RebuildRequired('checkpointed source unavailable')
            return tuple(found[rel] for rel in sorted(found))
        except OSError as exc:
            raise RebuildRequired('checkpointed source unavailable') from exc
        finally:
            os.close(root_fd)

    def _neighbors(self, c: MessageCopy) -> set[str]:
        if c.occurred_ms is None or c.batch_key or (not c.native_id and c.text is None):
            return set()
        prefix = c.channel, c.chat_id, _direction([c])
        texts = ({c.text} if c.text is not None else self._loose_texts.get(prefix, set())) if c.native_id else {c.text, None}
        result: set[str] = set()
        for text in texts:
            items = self._times.get((*prefix, text, not bool(c.native_id)), [])
            low = bisect_left(items, (c.occurred_ms - WINDOW_MS, '', ''))
            high = bisect_right(items, (c.occurred_ms + WINDOW_MS, chr(0x10ffff), chr(0x10ffff)))
            result.update(key for _, _, key in items[low:high])
        return result

    def _closure(self, changes: dict[str, Extracted]) -> set[str]:
        selected = set(changes)
        todo = list(selected)
        while todo:
            key = todo.pop()
            units = [ex for ex in (self._extracted.get(key), changes.get(key)) if ex is not None]
            related: set[str] = set()
            for ex in units:
                for c in ex.messages:
                    related.update(self._by_key.get(_msg_key(c), ()))
                    related.update(self._neighbors(c))
                    if c.native_id:
                        related.update(self._by_target.get((c.channel, c.chat_id, c.native_id), ()))
                    if key in changes:
                        cid = self._resolution.resolve(c.sender, occurred_ms=c.occurred_ms, time_basis=c.time_certainty)[0]
                        prior_ids = {mid for unit in self._by_key.get(_msg_key(c), ()) for mid in self._unit_rows.get(unit, ())}
                        named = [self._messages_rows[mid] for mid in prior_ids if self._messages_rows[mid].get('_sender_name')]
                        name_changed = any(row['sender_contact_id'] != cid or (c.sender_name and row['_sender_name'] != c.sender_name) for row in named)
                        new_name = not named and c.sender_name and cid not in self._chat_names.get((c.chat_id, c.sender_name), {})
                        if name_changed or new_name:
                            related.update(self._by_chat.get((c.channel, c.chat_id), ()))
                attachments: list[Description | MediaRecord] = [*ex.descriptions, *ex.media_records]
                for attachment in attachments:
                    related.update(self._by_key.get((attachment.channel, attachment.chat_id, attachment.native_id), ()))
                for event in ex.events:
                    related.update(self._by_target.get(_target_key(event), ()))
                    if event.target_native_id:
                        related.update(self._by_key.get(_target_key(event), ()))
                    # Purged assistant echoes may attach across native targets in this chat.
                    if event.kind == 'reaction':
                        for candidate_key in self._by_target.get((event.channel, event.chat_id, ''), ()):
                            related.add(candidate_key)
                        if not event.target_native_id and not event.payload.get('emoji') and event.occurred_ms is not None:
                            items = self._reaction_times.get((event.channel, event.chat_id), [])
                            low = bisect_left(items, (event.occurred_ms - 10_000, '', ''))
                            high = bisect_right(items, (event.occurred_ms + 10_000, chr(0x10ffff), chr(0x10ffff)))
                            related.update(other for _, _, other in items[low:high])
                for mid in self._unit_rows.get(key, ()):
                    for ref in self._messages_rows[mid]['source_refs']:
                        physical = ref.split('#')[0] + '#' + ref.split('#')[1].split('/')[0]
                        other = self._ref_unit.get(physical)
                        if other and _chats(self._extracted[other]):
                            related.add(other)
                for eid in self._unit_events.get(key, ()):
                    for ref in self._events_rows[eid]['source_refs']:
                        physical = ref.split('#')[0] + '#' + ref.split('#')[1].split('/')[0]
                        other = self._ref_unit.get(physical)
                        if other and _chats(self._extracted[other]):
                            related.add(other)
            for other in related - selected:
                selected.add(other)
                todo.append(other)
        return selected

    def _local(self, selected: set[str], changes: dict[str, Extracted]) -> Extracted:
        ex = Extracted()
        for key in sorted(selected):
            unit = changes.get(key, self._extracted.get(key))
            if unit is not None:
                for name in ('messages', 'events', 'descriptions', 'media_records'):
                    getattr(ex, name).extend(getattr(unit, name))
        relevant: dict[str, Any] = {}
        from .attestations import message_copy_id
        copies: list[MessageCopy | EventCopy] = [*ex.messages, *ex.events]
        for c in copies:
            locators = [c.ref, *c.extra_refs]
            if isinstance(c, MessageCopy):
                locators.append(message_copy_id(c))
                if c.segmented:
                    locators.append(c.ref.rsplit('/', 1)[0])
            for locator in locators:
                for att in self._author_refs.get(locator, ()):
                    relevant[att.ref] = att
        ex.attestations = list(relevant.values())
        return ex

    def _identity_plan(self, changes: dict[str, Extracted]) -> tuple[Resolution, IdentityInput | None, dict[Ident, Sighting], dict[int, Any], dict[int, Any]]:
        sightings: dict[Ident, Sighting] = {}
        slow = False
        for key, ex in changes.items():
            old = self._extracted.get(key)
            if old and _identity_evidence(old):
                raise RebuildRequired('identity contribution retraction requires rebuild')
            inp = ex.identity
            slow |= bool(inp.contact_records or ex.attestations or inp.groups - self._input.groups)
            for link in inp.links:
                a, b, evidence, _ = link
                aa = self._resolution._identifier_index.get((classify(a).kind, a), []) if classify(a) else []
                bb = self._resolution._identifier_index.get((classify(b).kind, b), []) if classify(b) else []
                slow |= (tuple(link[:3]) not in self._known_links or evidence != 'native_pair' or
                         len(aa) != 1 or len(bb) != 1 or aa[0].contact_id != bb[0].contact_id or
                         any(row.valid_from_ms is not None or row.valid_until_ms is not None for row in [*aa, *bb]))
            for node, names in inp.names.items():
                slow |= any(name not in self._known_names.get(node, ()) for _, name, _ in names)
                ident = classify(node)
                if ident is not None and ident.kind == 'numeric':
                    ident = self._resolution.canonical.get(ident.value, ident)
                bindings = self._resolution._identifier_index.get((ident.kind, ident.value), []) if ident else []
                slow |= len(bindings) != 1 or any(row.valid_from_ms is not None or row.valid_until_ms is not None for row in bindings)
                if len(bindings) == 1:
                    for ms, name, ref in names:
                        for row in self._idents_by_contact[bindings[0].contact_id]:
                            if row.kind == 'push_name' and row.value == name:
                                slow |= (ms is not None and (row.first_seen_ms is None or ms < row.first_seen_ms)) or (
                                    ms == row.first_seen_ms and bool(row.source_refs) and _ref_order(ref) < _ref_order(row.source_refs[0]))
            for ident, seen in inp.sightings.items():
                previous = sightings.get(ident, self._input.sightings.get(ident))
                if previous is None:
                    sightings[ident] = deepcopy(seen)
                    slow = True
                else:
                    merged = deepcopy(previous)
                    merged.merge(seen)
                    if seen.first_ms == previous.first_ms and seen.first_ref and previous.first_ref and _ref_order(seen.first_ref) < _ref_order(previous.first_ref):
                        merged.first_ref = seen.first_ref
                    sightings[ident] = merged
                    slow |= merged.first_ms != previous.first_ms or merged.first_ref != previous.first_ref
        replacements: dict[int, Any] = {}
        contact_replacements: dict[int, Any] = {}
        if not slow:
            res = copy(self._resolution)
            res.review = dict(self._resolution.review)
            for ident, seen in sightings.items():
                effective = self._resolution.canonical.get(ident.value, ident) if ident.kind == 'numeric' else ident
                for row in self._resolution._identifier_index.get((effective.kind, effective.value), ()):
                    updated = replace(row, last_seen_ms=max((v for v in (row.last_seen_ms, seen.last_ms) if v is not None), default=None))
                    if updated != row:
                        replacements[self._ident_positions[id(row)]] = updated
            for unit in changes.values():
                for a, b, _, ref in unit.identity.links:
                    cid = None
                    for value in (a, b):
                        ident = classify(value)
                        for row in self._resolution._identifier_index.get((ident.kind, ident.value), ()) if ident else ():
                            position = self._ident_positions[id(row)]
                            updated = replacements.get(position, row)
                            replacements[position] = replace(updated, source_refs=tuple(sorted(set(updated.source_refs) | {ref})[:MAX_REFS]))
                            cid = row.contact_id
                    if cid:
                        position = self._contact_positions[cid]
                        contact = contact_replacements.get(position, self._resolution.contacts[position])
                        updated_contact = replace(contact, source_refs=tuple(sorted(set(contact.source_refs) | {ref})[:MAX_REFS]))
                        contact_replacements[position] = updated_contact
                        # Generated aliases share their terminal contact's source evidence.
                        for value in (a, b):
                            import uuid

                            from .resolve import NAMESPACE
                            alias = str(uuid.uuid5(NAMESPACE, value))
                            alias_position = self._contact_positions.get(alias)
                            if alias_position is not None and alias != cid:
                                alias_row = self._resolution.contacts[alias_position]
                                if alias_row.merged_into == cid and alias_row.source_refs == self._resolution.contacts[position].source_refs:
                                    contact_replacements[alias_position] = replace(alias_row, source_refs=updated_contact.source_refs)
                for node, names in unit.identity.names.items():
                    cid = res.resolve(classify(node))[0]
                    if not cid:
                        continue
                    for ms, name, _ in names:
                        for row in self._idents_by_contact[cid]:
                            if row.kind == 'push_name' and row.value == name:
                                position = self._ident_positions[id(row)]
                                updated = replacements.get(position, row)
                                if ms is not None and (row.first_seen_ms is None or ms < row.first_seen_ms):
                                    raise RebuildRequired('earlier name sighting requires rebuild')
                                replacements[position] = replace(updated, last_seen_ms=max((v for v in (updated.last_seen_ms, ms) if v is not None), default=None))
                    if cid not in self._fixed_names:
                        push = [replacements.get(self._ident_positions[id(row)], row) for row in self._idents_by_contact[cid] if row.kind == 'push_name']
                        if push:
                            latest = max(push, key=lambda row: (row.last_seen_ms or -1, row.value)).value
                            position = self._contact_positions[cid]
                            contact_replacements[position] = replace(contact_replacements.get(position, self._resolution.contacts[position]), display_name=latest)
            replacements = {position: row for position, row in replacements.items() if row != self._resolution.identifiers[position]}
            contact_replacements = {position: row for position, row in contact_replacements.items() if row != self._resolution.contacts[position]}
            return res, None, sightings, replacements, contact_replacements
        inp = copy(self._input)
        inp.sightings = dict(self._input.sightings) | sightings
        inp.groups = set(self._input.groups)
        inp.links = list(self._input.links)
        inp.link_times = dict(self._input.link_times)
        inp.names = defaultdict(list, {k: list(v) for k, v in self._input.names.items()})
        inp.contact_records = dict(self._input.contact_records)
        inp.attestations = list(self._input.attestations)
        affected_values = {ident.value for ident in sightings}
        for ex in changes.values():
            other = ex.identity
            inp.groups.update(other.groups)
            inp.links.extend(other.links)
            inp.link_times.update(other.link_times)
            for node, names in other.names.items():
                inp.names[node].extend(names)
                inp.names[node].sort(key=lambda item: _ref_order(item[2]))
            for ref, contact in other.contact_records.items():
                inp.contact_records.setdefault(ref, contact)
            inp.attestations.extend(ex.attestations)
            for link in other.links:
                affected_values.update(link[:2])
        # Include existing identifier components and numeric aliases before checking old copies.
        for value in list(affected_values):
            affected_values.update(self._numeric_values.get(numeric_part(value), ()))
            cid = self._resolution.resolve(classify(value))[0]
            if cid:
                affected_values.update(row.value for row in self._idents_by_contact.get(cid, ()))
        old_units = {key for value in affected_values for key in self._copies_by_ident.get(value, ())}
        res = resolve(inp)
        if _identity_change(self._resolution, res, self._local(old_units, {})):
            raise RebuildRequired('identifier component or applicability changed')
        return res, inp, sightings, replacements, contact_replacements

    def _normalize(self, ex: Extracted, resolution: Resolution, removed: set[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, dict[str, list[Any]]]:
        res = copy(resolution)
        res.review = {}
        authors = _authors(ex, res)
        messages, unattached = _messages(ex, res, res.role_contact.get('assistant'), authors)
        mids = {m['message_id'] for m in messages}
        for c in ex.events:
            key = f'{c.channel}:{c.chat_id}:{c.target_native_id}'
            if key in self._messages_rows and key not in removed:
                mids.add(key)
        events = _events(ex, res, res.role_contact.get('assistant'), mids, res.review, authors)
        return messages, events, unattached, res.review

    def plan(self, lines: Sequence[Layer1Line]) -> ProjectionDelta:
        self._staged = None
        groups, changes = {}, {}
        for key, additions in _units(lines).items():
            prior = {line.ref: line for line in self._groups.get(key, ())}
            for line in additions:
                if line.ref in prior:
                    raise RebuildRequired('source ref replayed outside checkpoint')
                if line.ref.startswith('owner/') or (line.record or {}).get('kind') in ('contact_record', 'identifier_record', 'pair_record', 'name_record'):
                    raise RebuildRequired('owner or identity decision requires fenced rebuild')
                prior[line.ref] = line
            groups[key] = list(prior.values())
            changes[key] = extract(groups[key])
        res, identity, sightings, replacements, contact_replacements = self._identity_plan(changes)
        selected = self._closure(changes)
        old_ex, ex = self._local(selected, {}), self._local(selected, changes)
        if any(att.ref in self._unresolved_authors for att in ex.attestations):
            raise RebuildRequired('previously unresolved author target needs fenced repair')
        old_mids = {mid for key in selected for mid in self._unit_rows.get(key, ())}
        old_eids = {eid for key in selected for eid in self._unit_events.get(key, ())}
        _, _, old_unattached, old_review = self._normalize(old_ex, self._resolution, old_mids)
        messages, events, unattached, new_review = self._normalize(ex, res, old_mids)
        outcomes = {file: dict(counts) for file, counts in self._outcomes.items()}
        pending = dict(self._pending) if any(key.startswith('pair:') for key in changes) else self._pending
        reviews = dict(self._report['review'])
        def adjust_reviews(before: dict[str, list[Any]], after: dict[str, list[Any]]) -> None:
            for category in before.keys() | after.keys():
                if before.get(category, []) == after.get(category, []):
                    continue
                counts = Counter(canonical_json(item) for item in reviews.get(category, []))
                counts.subtract(canonical_json(item) for item in before.get(category, []))
                counts.update(canonical_json(item) for item in after.get(category, []))
                reviews[category] = [json.loads(item) for item, count in sorted(counts.items()) for _ in range(max(0, count))]
        for key, unit in changes.items():
            previous = self._extracted.get(key, Extracted())
            for sign, contribution in ((-1, previous), (1, unit)):
                for (file, outcome), count in contribution.outcomes.items():
                    dest = outcomes.setdefault(file, {})
                    dest[outcome] = dest.get(outcome, 0) + sign * count
                    if not dest[outcome]:
                        dest.pop(outcome)
            adjust_reviews(previous.review, unit.review)
            if key.startswith('pair:'):
                pending.pop(key[5:], None)
                pending.update(unit.pending_pairs)
        adjust_reviews(old_review, new_review)
        if identity is not None:
            for category in res.review:
                if category not in ('message_id_collisions', 'author_targets', 'unmatched_event_payloads', *ex.review):
                    reviews[category] = res.review[category]
        report = dict(self._report)
        report.update(outcomes=outcomes, review=reviews, messages=len(self._messages_rows) - len(old_mids) + len(messages),
                      events=len(self._events_rows) - len(old_eids) + len(events),
                      unattached_media=self._report['unattached_media'] - old_unattached + unattached)
        rows = ProjectionRows(res, messages, events, report)
        if identity is None:
            dirty = {row.contact_id for row in replacements.values()}
            identifiers = [replacements.get(self._ident_positions[id(row)], row) for cid in dirty
                           for row in self._idents_by_contact[cid]]
            rows._identity_rows = list(contact_replacements.values()), identifiers, dirty
        else:
            old_contacts = {row.contact_id: row for row in self._resolution.contacts}
            contacts = [row for row in res.contacts if old_contacts.get(row.contact_id) != row]
            old_identifiers: dict[str, list[Any]] = self._idents_by_contact
            new_identifiers: dict[str, list[Any]] = defaultdict(list)
            for row in res.identifiers:
                new_identifiers[row.contact_id].append(row)
            dirty = {cid for cid in old_identifiers.keys() | new_identifiers.keys()
                     if set(old_identifiers.get(cid, [])) != set(new_identifiers.get(cid, []))}
            rows._identity_rows = contacts, [row for cid in dirty for row in new_identifiers.get(cid, [])], dirty
            live = [c for c in res.contacts if c.merged_into is None]
            report.update(contacts=len(live), provisional_contacts=sum(c.status == 'provisional' for c in live),
                          merged_contacts=len(res.contacts) - len(live))
        delta = ProjectionDelta(rows, old_mids, old_eids, False, None, pending)
        self._staged = _Plan(delta, groups, changes, identity, sightings, replacements, contact_replacements)
        return delta

    def accept(self, delta: ProjectionDelta) -> None:
        staged = self._staged
        if staged is None or staged.delta is not delta or delta.requires_rebuild:
            raise ValueError('delta is not the current staged plan')
        for mid in delta.message_ids:
            self._index_row(self._messages_rows.pop(mid), False, False)
        for eid in delta.event_ids:
            self._index_row(self._events_rows.pop(eid), True, False)
        for key, unit in staged.units.items():
            old = self._extracted.get(key)
            if old is not None:
                self._index_unit(key, old, False)
            self._groups[key] = staged.groups[key]
            self._extracted[key] = unit
            self._index_unit(key, unit, True)
        for row in delta.rows.messages:
            self._messages_rows[row['message_id']] = row
            self._index_row(row, False, True)
        for row in delta.rows.events:
            self._events_rows[row['event_id']] = row
            self._index_row(row, True, True)
        if staged.identity is not None:
            self._input = staged.identity
            self._resolution = delta.rows.resolution
            self._identity_indexes()
        else:
            self._input.sightings.update(staged.sightings)
            for unit in staged.units.values():
                self._input.links.extend(unit.identity.links)
                self._input.link_times.update(unit.identity.link_times)
                for node, names in unit.identity.names.items():
                    self._input.names[node].extend(names)
            for position, row in staged.contact_replacements.items():
                self._resolution.contacts[position] = row
            for position, row in staged.replacements.items():
                old_row = self._resolution.identifiers[position]
                self._resolution.identifiers[position] = row
                self._ident_positions.pop(id(old_row))
                self._ident_positions[id(row)] = position
                for items in (self._resolution._identifier_index[(row.kind, row.value)], self._idents_by_contact[row.contact_id]):
                    items[items.index(old_row)] = row
        self._report = delta.rows.report
        self._outcomes = self._report['outcomes']
        self._pending = delta.pending_pairs
        self._staged = None


def apply_committed(conn: sqlite3.Connection, index: ProjectionIndex, raw_root: Path,
                    target: Sequence[SourceBoundary]) -> dict[str, Any]:
    if conn.in_transaction:
        raise ValueError('incremental apply requires an idle connection')
    if conn.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION:
        raise RebuildRequired('unsupported history schema')
    states = conn.execute('SELECT file, lines, end_offset, sha256, projector_version, state_json FROM projector_state').fetchall()
    if any(row[4] != PROJECTOR_VERSION for row in states):
        raise RebuildRequired('unsupported projector version')
    stored = tuple(sorted((SourceBoundary(*row[:4]) for row in states if row[0] != '@runtime'), key=lambda b: b.relative_path))
    if stored != index._boundaries:
        raise RebuildRequired('cache and database checkpoints disagree')
    runtime_rows = [row for row in states if row[0] == '@runtime']
    if len(runtime_rows) != 1:
        raise RebuildRequired('missing runtime checkpoint')
    runtime = json.loads(runtime_rows[0][5])
    target = tuple(sorted(target, key=lambda b: b.relative_path))
    if len({b.relative_path for b in target}) != len(target) or not index._files.keys() <= {b.relative_path for b in target}:
        raise RebuildRequired('target omits or duplicates a checkpoint')
    lines, files, blanks = [], {}, dict(index._blanks)
    for boundary in target:
        tail, count, state = _tail(raw_root, boundary, index._files.get(boundary.relative_path))
        lines.extend(tail)
        blanks[boundary.relative_path] = blanks.get(boundary.relative_path, 0) + count
        files[boundary.relative_path] = state
    delta = index.plan(lines)
    if delta.requires_rebuild:
        raise RebuildRequired(delta.reason or 'closure requires rebuild')
    report = delta.rows.report
    outcomes = report['outcomes']
    for rel, count in blanks.items():
        if count:
            outcomes.setdefault(rel, {})['skipped:blank'] = count
    report.update(files=len(target), blank_lines_skipped=blanks,
                  accounting={b.relative_path: {'lines': b.line_number,
                              'accounted': sum(outcomes.get(b.relative_path, {}).values())} for b in target})
    report['accounting_ok'] = all(v['lines'] == v['accounted'] for v in report['accounting'].values())
    if not report['accounting_ok']:
        index._staged = None
        raise RebuildRequired('physical accounting mismatch')
    runtime.update(pending_pairs=delta.pending_pairs, outcomes=outcomes,
                   review={key: len(items) for key, items in report['review'].items()}, status='ready')
    try:
        conn.execute('BEGIN IMMEDIATE')
        write_rows(conn, delta.rows, message_ids=delta.message_ids, event_ids=delta.event_ids)
        for boundary in target:
            conn.execute('INSERT INTO projector_state VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(file) DO UPDATE SET '
                         'lines=excluded.lines, end_offset=excluded.end_offset, sha256=excluded.sha256, '
                         'projector_version=excluded.projector_version, state_json=excluded.state_json',
                         (boundary.relative_path, boundary.line_number, boundary.end_offset,
                          boundary.prefix_sha256, PROJECTOR_VERSION, '{}'))
        conn.execute("UPDATE projector_state SET state_json=? WHERE file='@runtime'", (canonical_json(runtime),))
        conn.commit()
    except BaseException:
        conn.rollback()
        index._staged = None
        raise
    index.accept(delta)
    index._boundaries, index._blanks, index._files = target, blanks, files
    return report
