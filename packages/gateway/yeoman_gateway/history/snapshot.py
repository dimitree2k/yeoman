"""Disposable, same-user ProjectionIndex cache; raw Layer 1 remains authoritative."""
from __future__ import annotations

import copyreg
import hashlib
import json
import os
import pickle
import stat
import tempfile
from collections.abc import Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any, BinaryIO

from loguru import logger
from yeoman_shared import whatsapp_protocol
from yeoman_shared.raw_archive import records
from yeoman_shared.raw_archive.records import SourceBoundary, _no_symlinks, lock_file

from .incremental import ProjectionIndex, _check_file, _File
from .layer1 import SUBDIRS
from .schema import PROJECTOR_VERSION, SCHEMA_VERSION

FORMAT = 1
MAX_HEADER = 4 * 1024 * 1024
CHUNK = 1024 * 1024


class SnapshotRefusedError(ValueError):
    pass


def snapshot_path(db_path: Path) -> Path:
    return db_path.with_name(db_path.stem + '.projection-index.snapshot')


def fingerprint() -> str:
    digest = hashlib.sha256()
    # Hash the whole history package, including this format and all extraction dependencies.
    package = Path(__file__).parent
    for path in sorted(package.rglob('*.py')):
        digest.update(str(path.relative_to(package)).encode() + b'\0')
        digest.update(path.read_bytes())
    shared = Path(records.__file__).parent
    for path in sorted(shared.glob('*.py')):
        digest.update(path.name.encode() + b'\0')
        digest.update(path.read_bytes())
    digest.update(Path(whatsapp_protocol.__file__).read_bytes())
    return digest.hexdigest()


try:
    _IMPORTED_FINGERPRINT = fingerprint()
except Exception:
    _IMPORTED_FINGERPRINT = None  # An unavailable source tree disables this disposable cache.


def _header(boundaries: Sequence[SourceBoundary], generation: int) -> dict[str, Any]:
    code = fingerprint()
    if code != _IMPORTED_FINGERPRINT:
        raise SnapshotRefusedError('fingerprint')
    return {'format': FORMAT, 'projector_version': PROJECTOR_VERSION,
            'schema_version': SCHEMA_VERSION, 'fingerprint': code,
            'generation': generation, 'boundaries': [
                [b.relative_path, b.line_number, b.end_offset, b.prefix_sha256]
                for b in sorted(boundaries, key=lambda b: b.relative_path)]}


def _hash(source: BinaryIO) -> str:
    digest = hashlib.sha256()
    while data := source.read(CHUNK):
        digest.update(data)
    return digest.hexdigest()


def _sync_dir(path: Path) -> None:
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def invalidate_snapshot(db_path: Path) -> None:
    try:
        snapshot_path(db_path).unlink()
    except FileNotFoundError:
        return
    _sync_dir(db_path)


def save_snapshot(path: Path, index: ProjectionIndex, generation: int) -> None:
    if index._staged is not None:
        raise ValueError('snapshot while staged')
    # Hash objects cannot be pickled, and object IDs do not survive a restart.
    state = {key: value for key, value in vars(index).items()
             if key not in ('_files', '_ident_positions', '_payload_cache', '_accepted_payloads')}
    files = {rel: (f.boundary, f.inode, f.size, f.mtime) for rel, f in index._files.items()}
    header = _header(index._boundaries, generation)
    header['checksum'] = '0' * 64
    encoded = json.dumps(header, sort_keys=True).encode() + b'\n'
    if len(encoded) > MAX_HEADER:
        raise ValueError('snapshot header too large')
    # A save killed by the stop timeout leaves its temporary file behind; the sole writer
    # removes those before starting a new one.
    for stale in path.parent.glob('.projection-index-*'):
        stale.unlink(missing_ok=True)
    fd, name = tempfile.mkstemp(prefix='.projection-index-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'w+b') as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(encoded)
            pickler = pickle.Pickler(output, protocol=5)
            pickler.dispatch_table = copyreg.dispatch_table.copy()
            pickler.dispatch_table[MappingProxyType] = lambda value: (dict, (dict(value),))
            pickler.dump((state, files))
            del pickler  # Release its object memo before hashing the on-disk payload.
            output.flush()
            output.seek(len(encoded))
            header['checksum'] = _hash(output)
            output.seek(0)
            output.write(json.dumps(header, sort_keys=True).encode() + b'\n')
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _sync_dir(path)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_prefix(raw_root: Path, boundaries: Sequence[SourceBoundary], metadata: dict) -> dict[str, _File]:
    files = {}
    for b in boundaries:
        rel = b.relative_path
        parts = Path(rel).parts
        if (len(parts) != 2 or parts[0] not in SUBDIRS or not parts[1].endswith('.jsonl')
                or rel != '/'.join(parts) or rel in files or b.end_offset < 0 or b.line_number < 0):
            raise SnapshotRefusedError('prefix')
        boundary, inode, size, mtime = metadata[rel]
        if boundary != b:
            raise SnapshotRefusedError('payload')
        old = _File(b, None, inode, size, mtime)
        path = raw_root / rel
        _no_symlinks(path)
        fd = lock_file(path)
        try:
            before = os.fstat(fd)
            _check_file(old, before)
            digest, offset, lines, last = hashlib.sha256(), 0, 0, b''
            while offset < b.end_offset:
                data = os.pread(fd, min(CHUNK, b.end_offset - offset), offset)
                if not data:
                    raise SnapshotRefusedError('prefix')
                digest.update(data)
                lines += data.count(b'\n')
                offset += len(data)
                last = data[-1:]
            if (digest.hexdigest() != b.prefix_sha256 or lines != b.line_number
                    or (offset and last != b'\n')):
                raise SnapshotRefusedError('prefix')
            _check_file(_File(b, digest, (before.st_dev, before.st_ino),
                              before.st_size, before.st_mtime_ns), os.fstat(fd))
            after = path.stat()
            _check_file(old, after)
            files[rel] = _File(b, digest, inode, after.st_size, after.st_mtime_ns)
        finally:
            os.close(fd)
    if files.keys() != metadata.keys():
        raise SnapshotRefusedError('payload')
    return files


def load_snapshot(path: Path, raw_root: Path, boundaries: Sequence[SourceBoundary],
                  generation: int) -> ProjectionIndex | None:
    reason = 'load_error'
    try:
        reason = 'unsafe_symlink'
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise SnapshotRefusedError('unsafe_symlink')
        reason = 'unsafe_type'
        _no_symlinks(path)
        reason = 'io_error'
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as source:
            info = os.fstat(source.fileno())
            for valid, code in ((stat.S_ISREG(info.st_mode), 'unsafe_type'),
                                (info.st_uid == os.getuid(), 'unsafe_owner'),
                                (not info.st_mode & 0o022, 'unsafe_mode')):
                if not valid:
                    raise SnapshotRefusedError(code)
            reason = 'format'
            encoded = source.readline(MAX_HEADER + 1)
            if len(encoded) > MAX_HEADER or not encoded.endswith(b'\n'):
                raise SnapshotRefusedError('format')
            header = json.loads(encoded)
            reason = 'load_error'
            expected = _header(boundaries, generation)
            reason = 'format'
            for key, value in expected.items():
                if type(header.get(key)) is not type(value) or header.get(key) != value:
                    raise SnapshotRefusedError(key)
            offset = source.tell()
            reason = 'checksum'
            if _hash(source) != header.get('checksum'):
                raise SnapshotRefusedError('checksum')
            source.seek(offset)
            reason = 'unpickle'
            # Pickle trusts the current user: never load shared, writable or symlinked caches.
            state, metadata = pickle.load(source)
            reason = 'payload'
            index = ProjectionIndex.__new__(ProjectionIndex)
            index.__dict__.update(state)
            if (index._staged is not None or index._boundaries != tuple(sorted(
                    boundaries, key=lambda b: b.relative_path))):
                raise SnapshotRefusedError('payload')
            reason = 'prefix'
            index._files = _validate_prefix(raw_root, index._boundaries, metadata)
            index._ident_positions = {id(row): i for i, row in enumerate(index._resolution.identifiers)}
            index._payload_cache = {}
            for unit in index._extracted.values():
                for event in unit.events:
                    index._snapshot_key(event, index._payload_cache)
            index._accepted_payloads = dict(index._payload_cache)
            return index
    except FileNotFoundError:
        reason = 'missing' if reason == 'io_error' else reason
    except SnapshotRefusedError as exc:
        reason = str(exc)
    except Exception:
        pass
    logger.info('Projection index snapshot refused reason={}', reason)
    return None
