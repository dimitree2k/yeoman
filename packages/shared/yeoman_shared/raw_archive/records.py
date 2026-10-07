"""Low-level, append-only line I/O shared by every raw archive component.

Appends take an exclusive ``flock`` on the file. After acquiring it, the writer checks that
the path still points at the same inode: an owner purge replaces files atomically, and a
writer that waited on the old inode must reopen instead of appending to a deleted file.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

DIR_MODE = 0o700
OPEN_FILE_MODE = 0o600
CLOSED_FILE_MODE = 0o444
PURGE_DISPOSITION_LOCK = ".purge-disposition.lock"
_MONTH_STEM = re.compile(r"^\d{4}-\d{2}$")
TOMBSTONE = {"purged_version": 1}


@dataclass(frozen=True, slots=True)
class CommittedLine:
    relative_path: str
    line_number: int
    end_offset: int


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def ensure_private_dir(path: Path) -> None:
    if path.is_dir():
        return
    parent = path.parent
    if parent != path:
        ensure_private_dir(parent)
    try:
        path.mkdir(mode=DIR_MODE)
    except FileExistsError:
        if not path.is_dir():
            raise
    else:
        _fsync_directory(parent)


def lock_file(path: Path, *, create: bool = False) -> int:
    """Take an exclusive lock, retrying if an owner purge replaced the path."""
    if create:
        ensure_private_dir(path.parent)
    while True:
        try:
            flags = os.O_RDWR | os.O_CREAT if create else os.O_RDONLY
            fd = os.open(path, flags, OPEN_FILE_MODE)
        except FileNotFoundError:
            if create:
                continue
            raise
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            if os.fstat(fd).st_ino == os.stat(path).st_ino:
                return fd
        except BaseException:
            os.close(fd)
            raise
        os.close(fd)


def append_line(
    path: Path,
    line: str,
    *,
    mode: int = OPEN_FILE_MODE,
    coordination_lock: Path | None = None,
    should_append: Callable[[], bool] | None = None,
    on_committed: Callable[[int, int], None] | None = None,
) -> bool:
    """Append one line and fsync; return false when the locked owner check disposes it."""
    if "\n" in line or "\r" in line:
        raise ValueError("raw archive lines must not contain newlines")
    ensure_private_dir(path.parent)
    data = (line + "\n").encode("utf-8")
    coordinator_fd = (
        lock_file(coordination_lock, create=True) if coordination_lock is not None else None
    )
    try:
        if coordination_lock is not None and coordination_lock.name == PURGE_DISPOSITION_LOCK:
            recover_pending_append(coordination_lock.parent, path)
        while True:
            try:
                fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_EXCL, mode)
            except FileExistsError:
                try:
                    fd = os.open(path, os.O_RDWR | os.O_APPEND)
                except FileNotFoundError:
                    continue
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                if os.fstat(fd).st_ino != os.stat(path).st_ino:
                    continue  # replaced while we waited; reopen the current file
                if should_append is not None and not should_append():
                    return False
                size = os.fstat(fd).st_size
                if size and os.pread(fd, 1, size - 1) != b"\n":
                    if os.write(fd, b"\n") != 1:
                        raise OSError("could not separate an incomplete raw archive line")
                view = memoryview(data)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise OSError("raw archive append made no progress")
                    view = view[written:]
                os.fsync(fd)
                _fsync_directory(path.parent)
                if os.fstat(fd).st_ino != os.stat(path).st_ino:
                    raise OSError("raw archive path replaced after publication")
                if on_committed is not None:
                    end = os.fstat(fd).st_size
                    # ponytail: owner receipt scans file bytes; index counts if owner append frequency warrants it.
                    lines = 0
                    for offset in range(0, end, 1024 * 1024):
                        lines += os.pread(fd, min(1024 * 1024, end - offset), offset).count(b"\n")
                    on_committed(lines, end)
                return True
            finally:
                os.close(fd)
    finally:
        if coordinator_fd is not None:
            os.close(coordinator_fd)


def recover_pending_append(raw_root: Path, path: Path) -> None:
    """Caller holds the root lock; settle authorized purge bytes before taking the append lock."""
    # Local import: purge uses these I/O primitives and the writer that calls append_line.
    from .purge import _recover_pending

    _recover_pending(raw_root, now_ms=int(time.time() * 1000),
                     destination=path.relative_to(raw_root).as_posix())


def validate_owner_envelope(record: Mapping[str, Any]) -> None:
    """Publisher envelope contract, shared by whole-package validation and publication."""
    version = record.get('attestation_version')
    if (type(version) is not int or version not in (1, 2)
            or not isinstance(record.get('type'), str) or not record['type']):
        raise ValueError('invalid owner attestation envelope')


def preflight_owner_paths(raw_root: Path) -> None:
    """Read-only protected owner path checks before any lock/destination creation."""
    for relative in (PURGE_DISPOSITION_LOCK, 'AUDIT', 'owner/attestations.jsonl'):
        _no_symlinks(raw_root / relative)


def append_owner_record_locked(raw_root: Path, record: Mapping[str, Any]) -> CommittedLine | None:
    """Publish a gateway-validated owner envelope to its fixed destination, without a queue."""
    validate_owner_envelope(record)
    preflight_owner_paths(raw_root)
    relative = "owner/attestations.jsonl"
    path = raw_root / relative
    line = dumps(record)
    receipt = None

    def committed(number: int, end: int) -> None:
        nonlocal receipt
        receipt = CommittedLine(relative, number, end)

    recover_pending_append(raw_root, path)
    append_line(path, line,
                should_append=lambda: not _owner_is_disposed(raw_root, record, line),
                on_committed=committed)
    return receipt


def append_owner_record(raw_root: Path, record: Mapping[str, Any]) -> CommittedLine | None:
    """Publish one owner record while sharing the purge/import root lock."""
    validate_owner_envelope(record)
    preflight_owner_paths(raw_root)
    fd = lock_file(raw_root / PURGE_DISPOSITION_LOCK, create=True)
    try:
        return append_owner_record_locked(raw_root, record)
    finally:
        os.close(fd)


def _owner_is_disposed(raw_root: Path, record: Mapping[str, Any], line: str) -> bool:
    if append_is_disposed(raw_root / "AUDIT", dict(record), line):
        return True
    legacy = record.get('message_id')
    if record.get('type') == 'message_author' and isinstance(legacy, str):
        keys = legacy.split(':', 2)
        if len(keys) == 3 and append_is_disposed(raw_root / 'AUDIT', {
                'channel': keys[0], 'chat_id': keys[1], 'native_id': keys[2]}, line):
            return True
    ref = record.get('source_ref')
    if isinstance(ref, str):
        match = re.fullmatch(r'(whatsapp|backfill|derived)/([^/\\#]+\.jsonl)#([1-9][0-9]*)(?:/([0-9]+))?', ref)
        if match is None:
            raise ValueError('invalid owner source ref')
        sub, name, number, segment = match.groups()
        path = raw_root / sub / name
        _no_symlinks(path)
        try:
            selected = next((item for item in iter_records(path) if item[0] == int(number)), None)
        except FileNotFoundError:
            selected = None
        if selected is None:
            # Compatibility for the existing gateway-validated, explicitly identity-bound envelope.
            # A canonical source-ref-only author has no such binding and must refuse.
            if (isinstance(record.get('channel'), str) and record['channel']
                    and isinstance(record.get('chat_id'), str) and record['chat_id']
                    and any(isinstance(record.get(key), str) and record[key]
                            for key in ('native_message_id', 'native_id'))):
                return append_is_disposed(raw_root / 'AUDIT', dict(record), line)
            raise ValueError('owner source evidence unavailable')
        _, source, source_line = selected
        if source == TOMBSTONE:
            return True
        if not isinstance(source, dict):
            raise ValueError('owner source evidence invalid')
        payload = source.get('payload')
        parts = payload.get('segments') if isinstance(payload, dict) else None
        if isinstance(parts, list):
            if segment is None:
                native_id = payload.get('messageId')
                indexes = [index for index, part in enumerate(parts)
                           if native_id and isinstance(part, dict) and part.get('messageId') == native_id]
                if len(indexes) != 1:
                    raise ValueError('ambiguous owner base source ref')
                index = indexes[0]
            else:
                index = int(segment)
            if index >= len(parts) or not isinstance(parts[index], dict):
                raise ValueError('owner segment evidence unavailable')
            if parts[index] == TOMBSTONE:
                return True
            source = {'channel': source.get('channel'), 'chat_id': source.get('chat_id'),
                      'received_ms': record_capture_ms(source), 'payload': parts[index],
                      'origin': source.get('origin')}
            source_line = dumps(source)
        elif segment is not None:
            raise ValueError('owner source is not segmented')
        origin = source.get('origin')
        row_bound = isinstance(origin, dict) and isinstance(origin.get('row_sha256'), str)
        if (not isinstance(source.get('channel'), str) or not source['channel']
                or not isinstance(source.get('chat_id'), str) or not source['chat_id']
                or not (record_identities(source) or record_correlations(source)
                        or source.get('native_id') or row_bound)):
            raise ValueError('owner source identity unavailable')
        return append_is_disposed(raw_root / 'AUDIT', source, source_line)

    return False


def record_capture_ms(record: dict[str, Any]) -> int:
    """Source capture time, never a provider occurrence time or import execution time."""
    for key in ("received_ms", "generated_ms"):
        if record.get(key) is not None:
            return int(record[key])
    original = record.get("original")
    if isinstance(original, dict):
        for key in ("received_ms", "created_ms", "updated_ms"):
            if original.get(key) is not None:
                return int(original[key])
        value = original.get("created_at")
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                from datetime import UTC

                parsed = parsed.replace(tzinfo=UTC)
            return int(parsed.timestamp() * 1000)
    if record.get("time_certainty") == "capture_time_approx":
        return int(record.get("occurred_ms") or 0)
    return 0  # Unknown capture time cannot prove that content postdates a disposition.


def record_parts(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Independent segment identities; the parent native ID never selects other speakers."""
    payload = record.get("payload")
    segments = payload.get("segments") if isinstance(payload, dict) else None
    if not isinstance(segments, list):
        return [record]
    return [{"channel": record.get("channel"), "chat_id": record.get("chat_id"),
             "received_ms": record_capture_ms(record), "payload": segment}
            for segment in segments if isinstance(segment, dict) and segment != TOMBSTONE]


def record_identities(record: dict[str, Any]) -> set[str]:
    channel = str(record.get("channel") or "")
    kind = str(record.get("kind") or "")
    native = record.get("native")
    if isinstance(native, dict):
        event_metadata = (
            (channel == "telegram" and kind in {"update", "media"})
            or "eventId" in native
            or record.get("provenance") == "journal"
        )
        ids = set() if event_metadata else {str(record.get("native_id") or "")}
        payload = native.get("payload")
        if isinstance(payload, dict):
            ids.add(str(payload.get("messageId") or ""))
            encrypted_edit = payload.get("encryptedEdit")
            if (
                payload.get("observationOnly") is True
                and payload.get("observationType") == "encrypted_message_edit_undecoded"
                and isinstance(encrypted_edit, dict)
                and encrypted_edit.get("kind") == "secretEncryptedMessage"
            ):
                ids.add(str(payload.get("targetMessageId") or ""))
        for key in ("message_id", "source_message_id"):
            ids.add(str(native.get(key) or ""))
        for key in (
            "message",
            "edited_message",
            "channel_post",
            "edited_channel_post",
            "business_message",
            "edited_business_message",
        ):
            message = native.get(key)
            if isinstance(message, dict):
                ids.add(str(message.get("message_id") or ""))
    else:
        ids = {str(record.get("native_id") or "")}
    ids.add(str(record.get("native_message_id") or ""))
    payload = record.get("payload")
    if isinstance(payload, dict):
        ids.add(str(payload.get("messageId") or ""))
        for segment in payload.get("segments", []) if isinstance(payload.get("segments"), list) else []:
            if isinstance(segment, dict):
                ids.add(str(segment.get("messageId") or ""))
    original = record.get("original")
    if isinstance(original, dict):
        for key in ("message_id", "source_message_id", "provider_message_id", "r_provider_message_id"):
            ids.add(str(original.get(key) or ""))
        payload_json = original.get("payload_json")
        if isinstance(payload_json, str):
            try:
                original_payload = json.loads(payload_json)
            except ValueError:
                original_payload = None
            if isinstance(original_payload, dict):
                for key in ("message_id", "source_message_id", "provider_message_id"):
                    ids.add(str(original_payload.get(key) or ""))
    if (channel == "telegram" and kind == "media") or record.get("provenance") == "journal":
        ids.add(str(record.get("correlation_id") or ""))
    ids.discard("")
    return ids


def record_correlations(record: dict[str, Any]) -> set[str]:
    values = {str(record.get("correlation_id") or "")}
    original = record.get("original")
    if isinstance(original, dict):
        values.add(str(original.get("correlation_id") or ""))
    values.discard("")
    return values


def derived_from_disposed(record: dict[str, Any], identities: set[str] | frozenset[str]) -> bool:
    """Generation time cannot make a purged source message new again."""
    return (record.get("kind") in {"media_description", "media_transcript"}
            and bool(record_identities(record).intersection(identities)))


def append_is_disposed(audit_path: Path, record: dict[str, Any], line: str) -> bool:
    """Match a locked month append against durable owner purge dispositions."""
    channel = str(record.get("channel") or "")
    chat_id = str(record.get("chat_id") or "")
    received_ms = record_capture_ms(record)
    identities = record_identities(record)
    correlations = record_correlations(record)
    digest = line_sha256(line)
    disposed = False
    try:
        for _, audit, _ in iter_records(audit_path):
            if audit is None:
                raise OSError("raw archive AUDIT contains an invalid line")
            if "disposition" not in audit:
                continue  # Historical AUDIT records predate durable dispositions.
            disposition = audit["disposition"]
            if not isinstance(disposition, dict):
                raise OSError("raw archive AUDIT disposition is invalid")
            scope = disposition.get("scope")
            disposition_channel = disposition.get("channel")
            scoped_chat = disposition.get("chat_id")
            before_ms = disposition.get("before_ms")
            message_ids = disposition.get("message_identities")
            correlation_ids = disposition.get("correlation_ids")
            removed_hashes = audit.get("removed_sha256")
            if (
                scope not in {"chat", "message"}
                or not isinstance(disposition_channel, str)
                or (scoped_chat is not None and not isinstance(scoped_chat, str))
                or (
                    before_ms is not None
                    and (not isinstance(before_ms, int) or isinstance(before_ms, bool))
                )
                or (scope == "chat" and before_ms is None)
                or not isinstance(message_ids, list)
                or any(not isinstance(value, str) for value in message_ids)
                or not isinstance(correlation_ids, list)
                or any(not isinstance(value, str) for value in correlation_ids)
                or not isinstance(removed_hashes, list)
                or any(not isinstance(value, str) for value in removed_hashes)
            ):
                raise OSError("raw archive AUDIT disposition is malformed")
            if disposition_channel != channel:
                continue
            if scoped_chat is not None and scoped_chat != chat_id:
                continue
            if scope == "message" and scoped_chat is None and chat_id:
                continue  # Do not let an unscoped numeric ID collide across chats.
            if (before_ms is not None and received_ms >= before_ms
                    and not derived_from_disposed(record, set(message_ids))):
                continue
            if scope == "chat":
                if before_ms is None:
                    raise OSError("raw archive chat disposition has no cutoff")
                disposed = True
            if digest in removed_hashes:
                disposed = True
            if identities.intersection(message_ids):
                disposed = True
            if correlations.intersection(correlation_ids):
                disposed = True
    except FileNotFoundError:
        if audit_path.is_symlink():
            raise OSError("raw archive AUDIT symlink is broken")
        return False
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise OSError("could not read raw archive purge dispositions") from exc
    return disposed


def append_protected(path: Path, line: str) -> None:
    """Append to a ``0444`` bookkeeping file (MANIFEST, AUDIT, SUPPRESSIONS)."""
    if path.exists():
        os.chmod(path, OPEN_FILE_MODE)
        try:
            append_line(path, line)
        finally:
            os.chmod(path, CLOSED_FILE_MODE)
    else:
        append_line(path, line, mode=CLOSED_FILE_MODE)


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return {"__b64__": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, (set, frozenset, tuple)):
        return sorted(value, key=repr) if isinstance(value, (set, frozenset)) else list(value)
    if isinstance(value, Path):
        return str(value)
    return repr(value)


def dumps(record: Mapping[str, Any]) -> str:
    return json.dumps(
        record, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=_json_default
    )


def iter_records(path: Path) -> Iterator[tuple[int, dict[str, Any] | None, str]]:
    """Yield ``(line_number, record_or_None, raw_line)``; unparseable lines yield ``None``."""
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for number, raw in enumerate(handle, start=1):
            line = raw.rstrip("\n")
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                yield number, None, line
                continue
            yield number, parsed if isinstance(parsed, dict) else None, line


def file_digest(path: Path) -> tuple[str, int, int]:
    """``(sha256, line_count, byte_count)`` of a file, streamed."""
    digest = hashlib.sha256()
    lines = 0
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
            lines += chunk.count(b"\n")
    return digest.hexdigest(), lines, size


def line_sha256(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


def is_month_stem(stem: str) -> bool:
    return bool(_MONTH_STEM.match(stem))


def archive_files(root: Path, channel: str | None = None) -> list[Path]:
    """Every line file (month and seed files) below *root*, sorted; media excluded."""
    if not root.is_dir():
        return []
    if channel is not None:
        directories = [root / channel]
    else:
        directories = [p for p in sorted(root.iterdir()) if p.is_dir() and p.name != "media"]
    files: list[Path] = []
    for directory in directories:
        if directory.is_dir():
            files.extend(sorted(directory.glob("*.jsonl")))
    return files


IMPORT_RECEIPTS = '.import-receipts.jsonl'
_IMPORT_PATH = re.compile(r'backfill/[A-Za-z0-9_-]+\.jsonl|derived/media-descriptions\.jsonl')


def _no_symlinks(path: Path) -> None:
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('import path must not traverse a symlink')
    if path.exists() and (not path.is_file() and not path.is_dir()):
        raise ValueError('import path must be a regular file or directory')


def import_manifest_digest(manifest: Mapping[str, Any]) -> str:
    return line_sha256(dumps({k: v for k, v in manifest.items() if k != 'package_digest'}))


def import_source_locator(origin: Mapping[str, Any]) -> tuple[str | None, dict[str, Any], dict[str, Any]]:
    """Relative locator in a digest-bound logical source inventory; never read origin paths."""
    if not origin:
        return None, {}, {}
    path = origin.get('path')
    if not isinstance(path, str) or not path:
        raise ValueError('invalid source origin path')
    identity = line_sha256(dumps({'store': origin.get('store'), 'path': path}))
    entry = {'path': f'sources/{identity}/source', 'origin_path_sha256': line_sha256(path)}
    locator = {**origin, 'path': entry['path'], 'inventory_id': identity}
    return identity, entry, locator


def validate_import_manifest(staged_root: Path, manifest: Mapping[str, Any]) -> dict[str, bytes]:
    """Read and validate the entire fixed-destination envelope; never create files."""
    from .paths import is_protected

    _no_symlinks(staged_root)
    if is_protected(staged_root):
        raise ValueError('staging must be outside protected archives')
    if (manifest.get('version') != 1 or not isinstance(manifest.get('snapshot_identity'), str)
            or not manifest['snapshot_identity'] or not isinstance(manifest.get('files'), dict)
            or not manifest['files'] or manifest.get('package_digest') != import_manifest_digest(manifest)):
        raise ValueError('invalid import manifest')
    boundary = {'basis': 'staged-byte-prefixes', 'files': {
        name: {key: info.get(key) for key in ('sha256', 'bytes', 'lines')}
        for name, info in manifest['files'].items() if isinstance(info, dict)}}
    if manifest.get('snapshot_boundary') != boundary:
        raise ValueError('invalid snapshot boundary')
    paths = {p.relative_to(staged_root).as_posix() for p in staged_root.rglob('*.jsonl')}
    if paths != set(manifest['files']):
        raise ValueError('manifest must cover exactly the staged line files')
    result = {}
    ref_map = {}
    inventory = {}
    for relative, info in sorted(manifest['files'].items()):
        if not isinstance(relative, str) or _IMPORT_PATH.fullmatch(relative) is None:
            raise ValueError('unsupported import destination')
        path = staged_root / relative
        _no_symlinks(path)
        blob = path.read_bytes()
        lines = blob.splitlines(keepends=True)
        if (not isinstance(info, dict) or info.get('sha256') != hashlib.sha256(blob).hexdigest()
                or info.get('bytes') != len(blob) or info.get('lines') != len(lines)
                or (blob and not blob.endswith(b'\n')) or not isinstance(info.get('rows'), list)
                or len(info['rows']) != len(lines)):
            raise ValueError('staged file differs from manifest')
        for number, (line, row) in enumerate(zip(lines, info['rows'], strict=True), 1):
            record = json.loads(line)
            ref = f'{relative}#{number}'
            if (not isinstance(record, dict) or not isinstance(row, dict)
                    or row.get('source_ref') != ref or row.get('sha256') != hashlib.sha256(line).hexdigest()):
                raise ValueError('invalid staged row envelope')
            if relative.startswith('backfill/'):
                if record.get('backfill_version') != 1 or not isinstance(record.get('origin'), dict):
                    raise ValueError('invalid backfill envelope')
            elif record.get('kind') != 'media_description':
                raise ValueError('invalid derived envelope')
            origin = record.get('origin', {})
            inventory_id, entry, locator = import_source_locator(origin)
            if inventory_id is not None:
                inventory[inventory_id] = entry
            original = record.get('original')
            uuid = original.get('uuid', original.get('id')) if isinstance(original, dict) else None
            if (row.get('original_row_sha256') != origin.get('row_sha256') or row.get('uuid') != uuid
                    or row.get('origin') != locator):
                raise ValueError('manifest original locator differs from staged row')
            ref_map[ref] = ref
            payload = record.get('payload')
            if isinstance(payload, dict) and isinstance(payload.get('segments'), list):
                for index in range(len(payload['segments'])):
                    ref_map[f'{ref}/{index}'] = f'{ref}/{index}'
        result[relative] = blob
    if manifest.get('source_inventory') != inventory:
        raise ValueError('manifest source inventory differs from staged evidence')
    if manifest.get('ref_map') != ref_map:
        raise ValueError('manifest ref map differs from staged physical rows')
    return result


def _import_receipt(raw_root: Path, digest: str) -> dict[str, Any] | None:
    path = raw_root / IMPORT_RECEIPTS
    _no_symlinks(path)
    if not path.exists():
        return None
    latest = {}
    for _, row, _ in iter_records(path):
        if row is None or row.get('version') != 1 or row.get('status') not in ('partial', 'complete'):
            raise ValueError('invalid import receipt journal')
        latest[row['package_digest']] = row
    if any(key != digest and row['status'] == 'partial' for key, row in latest.items()):
        raise ValueError('another partial import must be completed first')
    return latest.get(digest)


def _import_render(raw_root: Path, record: dict[str, Any], line: bytes) -> tuple[bytes, bool]:
    payload = record.get('payload')
    segments = payload.get('segments') if isinstance(payload, dict) else None
    if isinstance(segments, list):
        parts = iter(record_parts(record))
        kept, changed = [], False
        for segment in segments:
            if isinstance(segment, dict) and segment != TOMBSTONE:
                part = next(parts)
                disposed = append_is_disposed(raw_root / 'AUDIT', part, dumps(part))
                kept.append(TOMBSTONE if disposed else segment)
                changed |= disposed
            else:
                kept.append(segment)
        if changed:
            replacement = TOMBSTONE
            if any(isinstance(part, dict) and part != TOMBSTONE and part for part in kept):
                replacement = {key: record[key] for key in (
                    'backfill_version', 'channel', 'kind', 'chat_id', 'occurred_ms', 'time_certainty',
                    'direction', 'provenance', 'skip_reason') if key in record}
                replacement['received_ms'] = record_capture_ms(record)
                replacement['payload'] = {'segments': kept}
                for key in ('chatJid', 'fromAssistant', 'messageId'):
                    if key in payload and (key != 'messageId' or any(
                            isinstance(part, dict) and part.get('messageId') == payload[key] for part in kept)):
                        replacement['payload'][key] = payload[key]
            return (dumps(replacement) + '\n').encode(), True
    disposed = append_is_disposed(raw_root / 'AUDIT', record, line[:-1].decode('utf-8'))
    return ((dumps(TOMBSTONE) + '\n').encode() if disposed else line), disposed


def _import_plan(raw_root: Path, blobs: Mapping[str, bytes], manifest: Mapping[str, Any],
                 receipt: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Pin all prefixes and exact append bytes before any protected mutation."""
    files, ref_map, additions = {}, {}, {}
    for relative, blob in sorted(blobs.items()):
        path = raw_root / relative
        _no_symlinks(path)
        pending = path.with_name(path.name + '.import-partial')
        _no_symlinks(pending)
        current = path.read_bytes() if path.exists() else b''
        previous = receipt['files'].get(relative) if receipt else None
        if previous is not None:
            base_size = previous['base_bytes']
            base = current[:base_size]
            if len(base) != base_size or hashlib.sha256(base).hexdigest() != previous['base_sha256']:
                raise ValueError('import destination prefix changed')
        else:
            if relative.startswith('backfill/') and path.exists():
                raise FileExistsError('backfill destination already exists without import receipt')
            base = current
        if base and not base.endswith(b'\n'):
            raise ValueError('import destination has an incomplete physical line')
        base_lines = base.count(b'\n')
        # Exact bytes, not semantic JSON/text matching: different evidence stays distinct.
        existing = {}
        if relative.startswith('derived/'):
            for number, line in enumerate(base.splitlines(keepends=True), 1):
                existing.setdefault(line, number)
        added = []
        row_hashes = []
        suppressed = 0
        for number, line in enumerate(blob.splitlines(keepends=True), 1):
            record = json.loads(line)
            rendered, disposed = _import_render(raw_root, record, line)
            suppressed += int(disposed)
            # Each suppressed source retains a distinct tombstone slot, never compact refs.
            final_number = existing.get(rendered) if not disposed else None
            if final_number is None:
                added.append(rendered)
                final_number = base_lines + len(added)
                if not disposed and relative.startswith('derived/'):
                    existing.setdefault(rendered, final_number)
            source_ref = f'{relative}#{number}'
            final_ref = f'{relative}#{final_number}'
            ref_map[source_ref] = final_ref
            payload = record.get('payload')
            if isinstance(payload, dict) and isinstance(payload.get('segments'), list):
                for index in range(len(payload['segments'])):
                    ref_map[f'{source_ref}/{index}'] = f'{final_ref}/{index}'
            row_hashes.append(hashlib.sha256(rendered).hexdigest())
        addition = b''.join(added)
        info = {'base_bytes': len(base), 'base_sha256': hashlib.sha256(base).hexdigest(),
                'bytes': len(base) + len(addition), 'sha256': hashlib.sha256(base + addition).hexdigest(),
                'lines': base_lines + len(added), 'row_hashes': row_hashes, 'suppressed': suppressed}
        if previous is not None and info != previous:
            raise ValueError('import disposition or plan changed')
        tail = current[len(base):]
        boundaries = {0}
        offset = 0
        for line in added:
            offset += len(line)
            boundaries.add(offset)
        if (not addition.startswith(tail) or len(tail) not in boundaries
                or (receipt and receipt['status'] == 'complete' and current != base + addition)):
            raise ValueError('import destination bytes changed')
        if pending.exists() and (receipt is None or not addition.startswith(pending.read_bytes())):
            raise ValueError('unbound or changed incomplete publication')
        files[relative] = info
        additions[relative] = addition[len(tail):]
    result = {'version': 1, 'package_digest': manifest['package_digest'],
              'snapshot_identity': manifest['snapshot_identity'], 'status': 'partial',
              'files': files, 'ref_map': ref_map}
    if receipt and (receipt['ref_map'] != ref_map or set(receipt['files']) != set(files)):
        raise ValueError('import receipt differs from package')
    return result, additions


def preview_import(raw_root: Path, staged_root: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Read-only preview. Confirm repeats these checks under the existing root lock."""
    blobs = validate_import_manifest(staged_root, manifest)
    _no_symlinks(raw_root)
    _no_symlinks(raw_root / PURGE_DISPOSITION_LOCK)
    _no_symlinks(raw_root / 'AUDIT')
    receipt = _import_receipt(raw_root, manifest['package_digest'])
    result, _ = _import_plan(raw_root, blobs, manifest, receipt)
    if receipt and receipt['status'] == 'complete':
        return receipt
    return result


def _publish_import_file(path: Path, addition: bytes, *, write_once: bool) -> None:
    """Durable write-once link for backfill; derived lines reuse locked append_line."""
    if not write_once:
        if not path.exists() and not addition:
            ensure_private_dir(path.parent)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, OPEN_FILE_MODE)
            os.fsync(fd)
            os.close(fd)
            _fsync_directory(path.parent)
        for line in addition.splitlines(keepends=True):
            append_line(path, line[:-1].decode('utf-8'))
        return
    ensure_private_dir(path.parent)
    if path.exists():
        if addition:
            raise ValueError('backfill publication already exists')
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
            _fsync_directory(path.parent)
        finally:
            os.close(fd)
        return
    pending = path.with_name(path.name + '.import-partial')
    _no_symlinks(pending)
    if pending.exists():
        prefix = pending.read_bytes()
        if not addition.startswith(prefix):
            raise ValueError('incomplete backfill publication differs')
        fd = lock_file(pending, create=True)
        try:
            os.lseek(fd, 0, os.SEEK_END)
            view = memoryview(addition[len(prefix):])
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError('backfill staging made no progress')
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
    else:
        fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, OPEN_FILE_MODE)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(addition)
            handle.flush()
            os.fsync(handle.fileno())
    os.link(pending, path)
    _fsync_directory(path.parent)
    pending.unlink()
    _fsync_directory(path.parent)


def import_backfill(raw_root: Path, staged_root: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Prevalidate, pin a durable partial receipt, and resume exact publications; not atomic."""
    # ponytail: holds one staged package in memory; stream pinned descriptors if package size warrants it.
    blobs = validate_import_manifest(staged_root, manifest)
    preview_import(raw_root, staged_root, manifest)  # Invalid packages/destinations create nothing.
    coordinator = lock_file(raw_root / PURGE_DISPOSITION_LOCK, create=True)
    try:
        for relative in blobs:
            recover_pending_append(raw_root, raw_root / relative)
        receipt = _import_receipt(raw_root, manifest['package_digest'])
        result, additions = _import_plan(raw_root, blobs, manifest, receipt)
        if receipt and receipt['status'] == 'complete':
            return receipt
        if receipt is None:
            append_line(raw_root / IMPORT_RECEIPTS, dumps(result))
        for relative, addition in additions.items():
            path = raw_root / relative
            _publish_import_file(path, addition, write_once=relative.startswith('backfill/'))
        result['status'] = 'complete'
        append_line(raw_root / IMPORT_RECEIPTS, dumps(result))
        return result
    finally:
        os.close(coordinator)
