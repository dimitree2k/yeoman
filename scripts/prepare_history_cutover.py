#!/usr/bin/env python3
"""Isolated preparation only. The coordinator supplies cutover-inputs.json in snapshot-home.

Version-1 input: legacy_rows, preserved_rows (see _history_cutover), capture_rows
(preserved order/completion proofs), conversion_digest, snapshot_digest. No real
inputs/defaults are embedded here. Output is a private refs-only manifest and v3
handover; stdout is closed aggregate JSON.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from collections import Counter
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from typing import Any

from yeoman_gateway.history.export import require_isolated_paths
from yeoman_gateway.history.layer1 import canonical_json, row_sha256
from yeoman_gateway.history.live import HistoryBoundary
from yeoman_gateway.history.queries import HistoryQueries
from yeoman_gateway.history.reader import HistoryReader
from yeoman_gateway.knowledge._history_capture import HistoryCaptureProducer
from yeoman_gateway.knowledge._history_cutover import (
    prepare_capture_inputs,
    prepare_legacy_alias_inputs,
)
from yeoman_gateway.knowledge._history_sources import build_history_source_aliases
from yeoman_gateway.knowledge._history_upgrade import upgrade_history_knowledge
from yeoman_gateway.knowledge.api import open_knowledge_store
from yeoman_gateway.knowledge.authority import EvidenceAudience
from yeoman_gateway.knowledge.models import SourceRef
from yeoman_gateway.knowledge.runtime import RuntimeKnowledgePolicy, RuntimeKnowledgeSources
from yeoman_gateway.policy.schema import PolicyConfig
from yeoman_shared.raw_archive.records import enumerate_committed

_ARGUMENTS = ("snapshot_home", "history_db", "knowledge_source", "knowledge_target",
              "policy_snapshot", "output_root")
_COUNTS = ("total", "mapped", "missing", "ambiguous", "changed", "purged_revoked",
           "other_channel", "candidate_copies", "statements", "jobs", "withheld_statements",
           "affected_jobs", "duplicate_observations", "unmapped_terminal_job_refs",
           "cited_reason_counts", "uncited_reason_counts")


def _affected_counts(rows, statements, jobs, aliases):
    channels = {(row['event_id'],row['revision']):row.get('channel') for row in rows}
    return dict(statements=len({r['statement_id'] for r in statements}), jobs=len(jobs),
        withheld_statements=len({r['statement_id'] for r in statements
            if (r['event_id'],r['revision']) not in aliases and channels[r['event_id'],r['revision']]=='whatsapp'}),
        affected_jobs=sum(any((r['event_id'],r['revision']) not in aliases
            and channels[r['event_id'],r['revision']]=='whatsapp' for r in json.loads(job['sources_json'])) for job in jobs))


def _guard(paths: list[Path]) -> None:
    if any(not p.is_absolute() for p in paths):
        raise ValueError("unsafe_paths")
    # The shared resolver guard calls ensure_dir; refuse bad paths before that I/O.
    homes = (Path("/home/dm/.yeoman"), Path.home() / ".yeoman",
             Path(os.environ.get("YEOMAN_HOME", Path.home() / ".yeoman")))
    protected = homes
    for path in paths:
        for candidate in (path, *(Path(str(path) + s) for s in ("-wal", "-shm", ".lock"))):
            if any(p.is_symlink() for p in (candidate, *candidate.parents)):
                raise ValueError("unsafe_paths")
            resolved = candidate.resolve()
            if any(resolved == root.resolve() or root.resolve() in resolved.parents for root in protected):
                raise ValueError("unsafe_paths")


def _private_json(path: Path, value: Any) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(canonical_json(value) + "\n")
        out.flush()
        os.fsync(out.fileno())


def prepare(args: argparse.Namespace) -> dict[str, Any]:
    paths = [getattr(args, name) for name in _ARGUMENTS]
    bundle_path = args.snapshot_home / "cutover-inputs.json"
    raw = args.snapshot_home / "raw"
    manifest_path = args.output_root / "legacy-alias-manifest.json"
    _guard([*paths, bundle_path, raw, manifest_path])
    # Metadata scan rejects symlink descendants before the first content read.
    for root, dirs, files in os.walk(raw, followlinks=False):
        _guard([Path(root) / name for name in (*dirs, *files)])
    require_isolated_paths(*paths, bundle_path, raw, manifest_path)
    if (args.knowledge_target == args.knowledge_source or args.output_root.exists()
            or args.knowledge_target.exists()):
        raise ValueError("occupied_output")
    bundle_blob = bundle_path.read_bytes()
    bundle = json.loads(bundle_blob)
    if (bundle.get("version") != 1
            or any(not isinstance(bundle.get(k), str) or len(bundle[k]) != 64
                   for k in ("snapshot_digest", "conversion_digest"))):
        raise ValueError("invalid_inputs")
    policy_blob = args.policy_snapshot.read_bytes()
    policy = RuntimeKnowledgePolicy(engine=PolicyConfig.model_validate(json.loads(policy_blob)))
    vector = enumerate_committed(raw)
    with closing(sqlite3.connect(f"{args.history_db.as_uri()}?mode=ro", uri=True)) as db:
        state = db.execute("SELECT state_json FROM projector_state WHERE file='@runtime'").fetchone()
        runtime = json.loads(state[0]) if state else {}
        if db.execute("PRAGMA user_version").fetchone()[0] != 4:
            raise ValueError("history_schema_mismatch")
        if runtime.get("status") != "ready":
            raise ValueError("history_not_ready")
        checkpoints = {row[0]: (row[1], row[2], row[3]) for row in db.execute(
            "SELECT file,lines,end_offset,sha256 FROM projector_state WHERE file<>'@runtime'")}
        if any(checkpoints.get(s.relative_path) != (s.line_number, s.end_offset, s.prefix_sha256) for s in vector):
            raise ValueError("history_vector_mismatch")
    reader = HistoryReader(args.history_db)
    snapshot = reader.open_snapshot(HistoryBoundary(runtime["generation"], vector))
    knowledge = None
    try:
        q = HistoryQueries(snapshot)
        rows, locators, counts = prepare_legacy_alias_inputs(
            queries=q, legacy_rows=bundle["legacy_rows"], preserved_rows=bundle["preserved_rows"])
        aliases, _ = build_history_source_aliases(queries=q, legacy_rows=rows, locators=locators)
        upgrade = upgrade_history_knowledge(source=args.knowledge_source, target=args.knowledge_target)
        legacy_authority = RuntimeKnowledgeSources()
        for row in rows:
            if row["cutover_status"] == "other_channel" and "source_audience_json" in row:
                source = SourceRef(**{k: row[k] for k in SourceRef.__dataclass_fields__})
                audience = (EvidenceAudience.author_only(snapshot_id=row.get("snapshot_id"))
                    if row["source_audience_json"] is None else
                    EvidenceAudience.known(set(json.loads(row["source_audience_json"])), snapshot_id=row.get("snapshot_id")))
                legacy_authority.register_source(source, audience, revoked=row.get("status") in ("revoked", "purged"))
        knowledge = open_knowledge_store(args.knowledge_target, workspace_id="history-cutover",
            source_authority=legacy_authority, policy_authority=policy, history_mode=True)
        store = knowledge._store
        with store.transaction():
            for key, alias in aliases.items():
                previous = knowledge.history_source_ledger.alias(key)
                record = knowledge.history_source_ledger.lookup(*key)
                if previous is not None and previous != alias:
                    raise ValueError("conflicting_existing_alias")
                if record is not None and (record.revoked or record.source != alias.issued):
                    raise ValueError("conflicting_existing_alias")
            knowledge.history_source_ledger.persist_aliases(aliases)
            jobs = [dict(row) for row in store.query("SELECT * FROM knowledge_jobs")]
            statements = store.query("SELECT statement_id,event_id,revision FROM knowledge_statement_sources")
            known_keys = {(r["event_id"], r["revision"]) for r in rows}
            required = {(r["event_id"], r["revision"]) for r in statements}
            required.update((r["event_id"], r["revision"]) for job in jobs for r in json.loads(job["sources_json"]))
            if not required <= known_keys:
                raise ValueError("incomplete_source_inventory")
            counts.update(_affected_counts(rows,statements,jobs,aliases))
            boundary = knowledge.capture_boundary()
            if boundary is None:
                raise ValueError("missing_legacy_boundary")
            capture_summary = {}
            inputs = prepare_capture_inputs(queries=q, legacy_boundary=boundary,
                legacy_rows=[*rows, *bundle["capture_rows"]], jobs=jobs, summary=capture_summary)
            counts["duplicate_observations"] = capture_summary["duplicate_observations"]
            counts["unmapped_terminal_job_refs"] = capture_summary["unmapped_terminal_job_refs"]
            for cited,label in ((True,"cited_reason_counts"),(False,"uncited_reason_counts")):
                counts[label] = dict(sorted(Counter(row['cutover_reason'] for row in rows
                    if row['cutover_status'] != 'mapped'
                    and (((row['event_id'],row['revision']) in required) == cited)).items()))
            handover = HistoryCaptureProducer(knowledge).prepare_handover(
                snapshot, legacy_boundary=boundary, **inputs)
        entries = []
        for row in rows:
            issued = {key: row[key] for key in SourceRef.__dataclass_fields__ if key in row}
            key = row["event_id"], row["revision"]
            alias = aliases.get(key)
            entry = {"issued": issued, "status": row["cutover_status"],
                     "reason": row.get("cutover_reason", row["cutover_status"]), "proofs": row["preserved_proofs"]}
            if alias is not None:
                value = asdict(alias)
                value["audience"]["members"] = sorted(alias.audience.members)
                value["audience"]["allowed"] = sorted(alias.audience.allowed)
                entry["alias"] = value
                entry["proven_fields"] = row["proven_fields"]
            entries.append(entry)
        manifest = {"version": 1, "inputs": {
            "snapshot_digest": bundle["snapshot_digest"], "conversion_digest": bundle["conversion_digest"],
            "bundle_digest": hashlib.sha256(bundle_blob).hexdigest(),
            "policy_digest": hashlib.sha256(policy_blob).hexdigest(),
            "raw_boundary_digest": row_sha256([asdict(s) for s in vector]),
            "schema_digest": row_sha256({"history": 4, "knowledge": upgrade["schema_version"],
                                         "knowledge_source": upgrade["source_digest"]})},
            "entries": entries, "handover": handover, "capture_summary": capture_summary,
            "cited_reason_counts": counts["cited_reason_counts"], "uncited_reason_counts": counts["uncited_reason_counts"]}
        manifest["digest"] = row_sha256(manifest)
        args.output_root.mkdir(mode=0o700)
        _private_json(manifest_path, manifest)
        return {"ok": True, "handover": True, **{k: counts[k] for k in _COUNTS}}
    finally:
        if knowledge is not None:
            knowledge.close()
        snapshot.close()
        reader.close()


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError("invalid_arguments")


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description=__doc__)
    for name in _ARGUMENTS:
        parser.add_argument("--" + name.replace("_", "-"), required=True, type=Path)
    try:
        args = parser.parse_args(argv)
    except ValueError:
        print(json.dumps({"ok": False, "error": "invalid_arguments"}, sort_keys=True))
        return 1
    try:
        try:
            _guard([getattr(args, name) for name in _ARGUMENTS])
        except ValueError:
            print(json.dumps({"ok": False, "error": "unsafe_paths"}, sort_keys=True))
            return 1
        report = prepare(args)
    except Exception:
        # Never echo exceptions, paths, SQL, identifiers or proof payloads.
        print(json.dumps({"ok": False, "error": "preparation_failed"}, sort_keys=True))
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
