"""Bounded owner-local repair protocol; no model/tool registration."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from yeoman_shared.raw_archive.records import (
    PURGE_DISPOSITION_LOCK,
    append_owner_record_locked,
    import_backfill,
    lock_file,
    owner_mutation_guard,
)

if TYPE_CHECKING:
    from .live import HistoryProjector

MAX_IPC_REQUEST_BYTES = 64 * 1024
MAX_OWNER_PACKAGE_BYTES = 16 * 1024 * 1024


def _read_package_bytes(package_path: Path) -> bytes:
    if (not package_path.is_absolute() or len(str(package_path).encode('utf-8')) > 4096):
        raise ValueError('INVALID_PACKAGE_LOCATOR')
    fd = os.open(package_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError('INVALID_PACKAGE_LOCATOR')
        if before.st_size > MAX_OWNER_PACKAGE_BYTES:
            raise ValueError('PACKAGE_TOO_LARGE')
        chunks, total = [], 0
        while total <= MAX_OWNER_PACKAGE_BYTES:
            part = os.read(fd, min(65536, MAX_OWNER_PACKAGE_BYTES + 1 - total))
            if not part:
                break
            chunks.append(part)
            total += len(part)
        if total > MAX_OWNER_PACKAGE_BYTES:
            raise ValueError('PACKAGE_TOO_LARGE')
        after = os.fstat(fd)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns) or total != after.st_size:
            raise ValueError('PACKAGE_CHANGED')
        data = b''.join(chunks)
        return data
    finally:
        os.close(fd)


def _load_pinned_bytes(package_path: Path, package_sha256: str) -> bytes:
    if not isinstance(package_sha256, str) or not re.fullmatch('[0-9a-f]{64}', package_sha256):
        raise ValueError('INVALID_PACKAGE_LOCATOR')
    data = _read_package_bytes(package_path)
    if hashlib.sha256(data).hexdigest() != package_sha256:
        raise ValueError('PACKAGE_DIGEST_MISMATCH')
    return data


def _parse_owner_package(data: bytes) -> list[dict[str, Any]]:
    from yeoman_shared.raw_archive.records import validate_owner_envelope

    from .attestations import _check

    try:
        records = [json.loads(line) for line in data.decode('utf-8').splitlines()]
        if not records:
            raise ValueError('INVALID_PACKAGE')
        for record in records:
            if not isinstance(record, dict):
                raise ValueError('INVALID_PACKAGE')
            _check(record)
            validate_owner_envelope(record)
        return records
    except (UnicodeError, json.JSONDecodeError, TypeError, KeyError):
        raise ValueError('INVALID_PACKAGE') from None


def load_pinned_owner_package(package_path: Path, package_sha256: str) -> list[dict[str, Any]]:
    return _parse_owner_package(_load_pinned_bytes(package_path, package_sha256))


def validate_control(operation: str, args: Mapping[str, Any]) -> None:
    fields = {'status': set(), 'rebuild': {'confirm'},
              'attest': {'confirm', 'package_path', 'package_sha256'},
              'purge': {'confirm', 'selector'},
              'import-backfill': {'confirm', 'package_path', 'package_sha256', 'staged_path'}}
    if not isinstance(operation, str) or operation not in fields or not isinstance(args, Mapping) or set(args) != fields[operation]:
        raise ValueError('INVALID_OPERATION')
    if operation != 'status' and args['confirm'] is not True:
        raise ValueError('CONFIRM_REQUIRED')
    for key in ('package_path', 'package_sha256', 'staged_path'):
        if key in args and not isinstance(args[key], str):
            raise ValueError('INVALID_PACKAGE_LOCATOR')
    if 'staged_path' in args and not Path(args['staged_path']).is_absolute():
        raise ValueError('INVALID_PACKAGE_LOCATOR')
    if 'selector' in args:
        selector = args['selector']
        if (not isinstance(selector, dict) or set(selector) != {'channel', 'chat_id', 'native_id', 'before_ms'} or
                not isinstance(selector['channel'], str) or
                any(selector[k] is not None and not isinstance(selector[k], str) for k in ('chat_id', 'native_id')) or
                (selector['before_ms'] is not None and type(selector['before_ms']) is not int)):
            raise ValueError('INVALID_SELECTOR')
        from yeoman_shared.raw_archive.purge import PurgeSelector
        try:
            PurgeSelector(**selector).validate()
        except ValueError:
            raise ValueError('INVALID_SELECTOR') from None


async def request_history_control(socket_path: Path, operation: str,
                                  args: Mapping[str, Any]) -> dict[str, Any]:
    validate_control(operation, args)
    data = json.dumps({'cmd': 'history_control', 'args': {'operation': operation, **args}}).encode('utf-8') + b'\n'
    if len(data) > MAX_IPC_REQUEST_BYTES:
        raise ValueError('REQUEST_TOO_LARGE')
    writer = None
    try:
        async with asyncio.timeout(360):
            reader, writer = await asyncio.open_unix_connection(str(socket_path), limit=MAX_IPC_REQUEST_BYTES)
            writer.write(data)
            await writer.drain()
            response = json.loads(await reader.readline())
            if not isinstance(response, dict):
                raise ValueError('INVALID_RESPONSE')
            return response
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass


def projection_owned(raw_root: Path) -> bool:
    from yeoman_shared.raw_archive.records import PROJECTION_OWNER_LOCK
    if not (raw_root / PROJECTION_OWNER_LOCK).exists():
        return False
    try:
        with owner_mutation_guard(raw_root):
            return False
    except PermissionError:
        return True


def cli_control(operation: str, args: Mapping[str, Any]) -> dict[str, Any]:
    import typer
    from yeoman_shared.config.loader import load_config
    try:
        response = asyncio.run(request_history_control(Path(load_config().ipc.gateway_socket_path).expanduser(), operation, args))
    except (OSError, ValueError, TimeoutError):
        raise typer.BadParameter('Gateway history control unavailable; no direct mutation attempted') from None
    if response.get('status') != 'ok':
        raise typer.BadParameter('Gateway history control refused: ' + str(response.get('code', response.get('status'))))
    return response


async def control_projector(projector: HistoryProjector, operation: str,
                            args: Mapping[str, Any]) -> dict[str, Any]:
    from .attestations import validate_owner_package
    try:
        validate_control(operation, args)
        if projector.health()['status'] == 'disabled':
            return {'status': 'disabled'}
        if operation == 'status':
            return {'status': 'ok', 'health': projector.health()}
        mutation: Callable[[int], None] | None = None
        counts: dict[str, Any] = {}
        if operation == 'attest':
            path, digest = Path(args['package_path']), args['package_sha256']
            records = await projector._submit(load_pinned_owner_package, path, digest)
            await projector._submit(validate_owner_package, projector.raw_root, records)
            def attest_mutation(fd: int) -> None:
                records = load_pinned_owner_package(path, digest)
                lock = lock_file(projector.raw_root / PURGE_DISPOSITION_LOCK, create=True)
                try:
                    validate_owner_package(projector.raw_root, records)
                    committed = sum(append_owner_record_locked(projector.raw_root, record,
                        projection_owner_fd=fd, on_committed=projector.notify_committed) is not None for record in records)
                    counts.update(validated=len(records), committed=committed, suppressed=len(records) - committed)
                finally:
                    os.close(lock)
            mutation = attest_mutation
        elif operation == 'purge':
            import pwd
            from dataclasses import asdict

            from yeoman_shared.raw_archive.purge import PurgeSelector, purge
            selector = PurgeSelector(**args['selector'])
            def purge_mutation(fd: int) -> None:
                counts.update(asdict(purge(projector.raw_root, selector, operator=pwd.getpwuid(os.getuid()).pw_name,
                                          projection_owner_fd=fd)))
            mutation = purge_mutation
        elif operation == 'import-backfill':
            from .convert.run import prepare_import_manifest
            source = Path(args['staged_path'])
            path, digest = Path(args['package_path']), args['package_sha256']
            manifest = json.loads(await projector._submit(_load_pinned_bytes, path, digest))
            if manifest != await projector._submit(prepare_import_manifest, source):
                raise ValueError('INVALID_PACKAGE')
            def import_mutation(fd: int) -> None:
                captured = json.loads(_load_pinned_bytes(path, digest))
                if captured != prepare_import_manifest(source):
                    raise ValueError('INVALID_PACKAGE')
                result = import_backfill(projector.raw_root, source, captured,
                    projection_owner_fd=fd, on_committed=projector.notify_committed)
                if result.get('status') != 'complete':
                    raise ValueError('PARTIAL_IMPORT')
                counts.update(files=len(result['files']))
            mutation = import_mutation
        await projector.rebuild(reason='owner_request', mutation=mutation)
        # Purge's detailed file receipts stay private; the socket only carries aggregates.
        if operation == 'purge':
            counts = {k: v for k, v in counts.items() if isinstance(v, int)}
        return {'status': 'ok', **counts}
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        code = str(exc)
        safe = {'PACKAGE_DIGEST_MISMATCH', 'PACKAGE_TOO_LARGE', 'PACKAGE_CHANGED', 'INVALID_PACKAGE_LOCATOR',
                'INVALID_PACKAGE', 'INVALID_OPERATION', 'CONFIRM_REQUIRED', 'INVALID_SELECTOR', 'PARTIAL_IMPORT'}
        return {'status': 'error', 'code': code if code in safe else 'HISTORY_CONTROL_FAILED'}
