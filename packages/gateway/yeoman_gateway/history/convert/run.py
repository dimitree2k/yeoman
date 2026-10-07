"""Run every converter once and write frozen Layer 1 files (offline; never into data/raw)."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yeoman_shared.raw_archive.paths import is_protected
from yeoman_shared.utils.helpers import get_data_path, get_operational_store_path

from ..layer1 import write_jsonl_once
from .bridge_refs import Decoder, convert_bridge_refs
from .identity_stores import convert_chat_registry, convert_contacts_db, convert_knowledge
from .inbound_db import convert_inbound_db
from .journal import convert_journal
from .media import convert_media_descriptions, convert_media_records
from .memory_nodes import convert_memory_nodes
from .session_jsonl import convert_session_jsonl


@dataclass(frozen=True)
class Job:
    name: str
    subdir: str
    lines: Callable[[], Iterable[dict[str, Any]]]


def jobs(source_home: Path, *, decode: Decoder | None,
         extra_bridge_dirs: Sequence[Path] = ()) -> list[Job]:
    h = source_home
    bridge_dirs = [
        get_operational_store_path("bridge_references", data_dir=h / "data"),
        *extra_bridge_dirs,
    ]
    result = [
        Job("journal", "backfill", lambda: convert_journal(h)),
        Job("bridge_refs", "backfill", lambda: convert_bridge_refs(bridge_dirs, decode)),
        Job("reply_context", "backfill",
            lambda: convert_inbound_db(h, "data/inbound/reply_context.db", "reply_context")),
        Job("inbound_archive", "backfill",
            lambda: convert_inbound_db(h, "data/inbound/archive.db", "inbound_archive")),
        Job("session_jsonl", "backfill", lambda: convert_session_jsonl(h)),
        Job("memory", "backfill", lambda: convert_memory_nodes(h, "data/memory/memory.db", "memory")),
        Job("knowledge_memory", "backfill",
            lambda: convert_memory_nodes(h, "data/knowledge/knowledge.db", "knowledge_memory")),
    ]
    backups = sorted((h / "data/memory/backups").glob("memory-*-pre-rebackfill.db"))
    for index, backup in enumerate(backups, start=1):
        name = "memory_pre_rebackfill" if index == 1 else f"memory_pre_rebackfill_{index}"
        rel = backup.relative_to(h).as_posix()
        result.append(Job(name, "backfill",
                          lambda rel=rel, name=name: convert_memory_nodes(h, rel, name)))
    result += [
        Job("knowledge", "backfill", lambda: convert_knowledge(h)),
        Job("contacts_db", "backfill", lambda: convert_contacts_db(h)),
        Job("chat_registry", "backfill", lambda: convert_chat_registry(h)),
        Job("document_cache", "backfill", lambda: convert_media_records(h)),
        Job("media-descriptions", "derived", lambda: convert_media_descriptions(h)),
    ]
    return result


def check_paths(source_home: Path, raw_out: Path) -> None:
    if is_protected(raw_out):
        raise PermissionError(f"refusing to write into the protected raw archive: {raw_out}")
    if source_home.resolve() == get_data_path().resolve():
        raise PermissionError("source home must be a snapshot, not the live runtime home")


def run_conversion(source_home: Path, raw_out: Path, *, decode: Decoder | None,
                   extra_bridge_dirs: Sequence[Path] = ()) -> dict[str, Any]:
    check_paths(source_home, raw_out)
    todo = jobs(source_home, decode=decode, extra_bridge_dirs=extra_bridge_dirs)
    targets = [raw_out / job.subdir / f"{job.name}.jsonl" for job in todo]
    for target in targets:
        partial = target.with_name(target.name + ".partial")
        if target.exists() or target.is_symlink() or partial.exists() or partial.is_symlink():
            raise FileExistsError(target)
    report: dict[str, Any] = {"source_home": str(source_home), "raw_out": str(raw_out), "files": {}}
    for job, target in zip(todo, targets, strict=True):
        kinds: Counter[str] = Counter()
        skipped: Counter[str] = Counter()

        def counted(lines: Iterable[dict[str, Any]]) -> Iterator[dict[str, Any]]:
            for line in lines:
                kinds[str(line.get("kind"))] += 1
                if line.get("skip_reason"):
                    skipped[str(line["skip_reason"])] += 1
                yield line

        count = write_jsonl_once(target, counted(job.lines()))
        report["files"][f"{job.subdir}/{job.name}.jsonl"] = {
            "lines": count, "kinds": dict(sorted(kinds.items())), "skipped": dict(sorted(skipped.items())),
        }
    return report


def prepare_import_manifest(staged_root: Path) -> dict[str, Any]:
    """Bind staged physical rows and exact original locators to a deterministic snapshot."""
    import hashlib
    import json

    from yeoman_shared.raw_archive.records import (
        import_manifest_digest,
        import_source_locator,
        validate_import_manifest,
    )

    from ..layer1 import PROVENANCE, TIME_CERTAINTY, row_sha256

    if is_protected(staged_root):
        raise PermissionError('staging must be outside protected archives')
    if any(p.is_symlink() for p in (staged_root, *staged_root.parents)):
        raise ValueError('staging must not traverse symlinks')
    files, ref_map, inventory = {}, {}, {}
    for path in sorted(staged_root.rglob('*.jsonl')):
        relative = path.relative_to(staged_root).as_posix()
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError('staged file must not traverse symlinks')
        blob = path.read_bytes()
        rows = []
        for number, line in enumerate(blob.splitlines(keepends=True), 1):
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError('staged row must be an object')
            origin = record.get('origin', {})
            if not isinstance(origin, dict):
                raise ValueError('invalid origin')
            if relative.startswith('backfill/'):
                if (record.get('backfill_version') != 1 or record.get('provenance') not in PROVENANCE
                        or record.get('time_certainty') not in TIME_CERTAINTY
                        or not isinstance(record.get('payload'), dict)
                        or not isinstance(record.get('channel'), str) or not isinstance(record.get('kind'), str)
                        or not all(isinstance(origin.get(k), str) for k in ('store', 'path', 'table', 'row_key'))
                        or 'original' not in record):
                    raise ValueError('invalid backfill record')
            elif (record.get('kind') != 'media_description'
                    or not isinstance(record.get('channel'), str)
                    or not isinstance(record.get('chat_id'), str)
                    or not isinstance(record.get('native_message_id'), str)
                    or record.get('text') is not None and not isinstance(record['text'], str)):
                raise ValueError('invalid derived record')
            if 'original' in record and origin.get('row_sha256') != row_sha256(record['original']):
                raise ValueError('original row digest mismatch')
            ref = f'{relative}#{number}'
            original = record.get('original')
            uuid = original.get('uuid', original.get('id')) if isinstance(original, dict) else None
            inventory_id, entry, locator = import_source_locator(origin)
            if inventory_id is not None:
                inventory[inventory_id] = entry
            rows.append({'source_ref': ref, 'sha256': hashlib.sha256(line).hexdigest(),
                         'original_row_sha256': origin.get('row_sha256'), 'uuid': uuid,
                         'origin': locator})
            ref_map[ref] = ref
            payload = record.get('payload')
            if isinstance(payload, dict) and isinstance(payload.get('segments'), list):
                for index in range(len(payload['segments'])):
                    ref_map[f'{ref}/{index}'] = f'{ref}/{index}'
        files[relative] = {'sha256': hashlib.sha256(blob).hexdigest(), 'bytes': len(blob),
                           'lines': len(rows), 'rows': rows}
    manifest = {'version': 1, 'snapshot_identity': row_sha256({'files': files, 'source_inventory': inventory}),
                'source_inventory': inventory, 'files': files,
                'snapshot_boundary': {'basis': 'staged-byte-prefixes', 'files': {
                    name: {key: info[key] for key in ('sha256', 'bytes', 'lines')}
                    for name, info in files.items()}}, 'ref_map': ref_map}
    manifest['package_digest'] = import_manifest_digest(manifest)
    validate_import_manifest(staged_root, manifest)
    return manifest
