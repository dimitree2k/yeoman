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
