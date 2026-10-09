"""Synthetic preparation proofs; no runtime stores or provider calls."""
# ruff: noqa: F811
import copy
import importlib.util
import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from yeoman_gateway.history.layer1 import row_sha256
from yeoman_gateway.history.queries import HistoryQueries
from yeoman_gateway.knowledge._history_sources import build_history_source_aliases
from yeoman_gateway.knowledge.models import SourceRef, TrustedCaptureContext

from tests.gateway.convhist.test_hist_queries import event, message
from tests.gateway.test_history_capture_continuity import (
    GROUP,
    MS,
    issue,
    producer,
    worker,
)
from tests.gateway.test_history_capture_continuity import case as capture_case  # noqa: F401
from tests.gateway.test_history_source_compatibility import case  # noqa: F401


def proof(q, mid, source, *, created=10):
    row = q.message(mid)
    fields = ("channel", "chat_id", "native_message_id", "direction", "sent_ms",
              "time_certainty", "text", "current_text", "media_json", "reply_to_native_id",
              "mentions_json", "provenance", "deleted")
    state = {key: row[key] for key in fields}
    for key in ("media_json", "mentions_json"):
        state[key] = json.loads(state[key]) if state[key] is not None else None
    state["events"] = []
    original = {"issued": asdict(source), "state": state, "created_ms": created}
    return {"event_id": source.event_id, "revision": source.revision, "original": original,
            "origin": {"store": "synthetic", "path": "copy.db", "table": "events",
                       "row_key": source.event_id, "row_sha256": row_sha256(original)},
            "source_ref": "backfill/journal.jsonl#1"}


def legacy(source, **extra):
    return {**asdict(source), "author_contact_id": "a", "source_audience_json": None,
            "status": "active", **extra}


def test_cutover_alias_requires_unique_locator_and_original_proof(case):
    from yeoman_gateway.knowledge._history_cutover import prepare_legacy_alias_inputs
    _, q, _, db = case
    db.execute("UPDATE messages SET native_message_id='shared' WHERE message_id='m'")
    message(db, "foreign", chat="foreign@g.us")
    db.execute("UPDATE messages SET native_message_id='shared' WHERE message_id='foreign'")
    source = SourceRef("old", 3, "whatsapp", q.message("m")["chat_id"], "whatsapp:10001", 100)
    good = proof(q, "m", source)
    changed = replace(source, event_id="changed")
    changed_proof = proof(q, "m", changed)
    changed_proof["original"]["state"]["text"] = "Original different bytes"
    changed_proof["origin"]["row_sha256"] = row_sha256(changed_proof["original"])
    conflict = replace(source, event_id="conflict")
    first = proof(q, "m", conflict)
    second = proof(q, "foreign", replace(conflict, chat_id="foreign@g.us"))
    rows, locators, report = prepare_legacy_alias_inputs(
        queries=q, legacy_rows=[legacy(source), legacy(changed), legacy(conflict)],
        preserved_rows=[good, copy.deepcopy(good), changed_proof, first, second])
    aliases, _ = build_history_source_aliases(queries=q, legacy_rows=rows, locators=locators)
    assert aliases[source.key].issued == source
    assert aliases[source.key].message_id == "m"
    assert conflict.key not in aliases and changed.key not in aliases
    assert report["total"] == sum(report[key] for key in (
        "mapped", "missing", "ambiguous", "changed", "purged_revoked", "other_channel"))
    assert report["candidate_copies"] == 5 and report["ambiguous"] == 1 and report["changed"] == 1
    assert prepare_legacy_alias_inputs(queries=q, legacy_rows=reversed([legacy(source), legacy(changed), legacy(conflict)]),
                                      preserved_rows=reversed([good, good, changed_proof, first, second])) == (rows, locators, report)
    with pytest.raises(ValueError, match="conflicting_legacy_key"):
        prepare_legacy_alias_inputs(queries=q, legacy_rows=[legacy(source), legacy(source, author_contact_id="b")],
                                   preserved_rows=[good])
    missing = copy.deepcopy(good)
    del missing["original"]["state"]["media_json"]
    missing["origin"]["row_sha256"] = row_sha256(missing["original"])
    assert prepare_legacy_alias_inputs(queries=q, legacy_rows=[legacy(source)], preserved_rows=[missing])[2]["missing"] == 1
    broken = copy.deepcopy(good)
    broken["origin"]["row_sha256"] = "bad"
    assert prepare_legacy_alias_inputs(queries=q, legacy_rows=[legacy(source)], preserved_rows=[broken])[2]["missing"] == 1


def test_cutover_alias_never_widens_or_revives(case):
    from yeoman_gateway.knowledge._history_cutover import prepare_legacy_alias_inputs
    _, q, authority, db = case
    source = SourceRef("old", 3, "whatsapp", q.message("m")["chat_id"], "whatsapp:10001", 100)
    revoked = replace(source, event_id="revoked")
    reassigned = replace(source, event_id="reassigned", author_principal="whatsapp:10002")
    rows, locators, report = prepare_legacy_alias_inputs(
        queries=q, legacy_rows=[legacy(source), legacy(revoked, status="revoked"), legacy(reassigned)],
        preserved_rows=[proof(q, "m", s) for s in (source, revoked, reassigned)])
    aliases, _ = build_history_source_aliases(queries=q, legacy_rows=rows, locators=locators)
    authority.ledger.persist_aliases(aliases)
    assert aliases[source.key].audience.status == "author_only"
    assert authority.ledger.alias(source.key).audience == aliases[source.key].audience
    event(db, "later", "member_add", 200, {"participants": [["10003@s.whatsapp.net"]]})
    assert not authority.permits_principal(source, "whatsapp:10003", now_ms=300)
    assert revoked.key not in aliases and reassigned.key not in aliases
    assert report["purged_revoked"] == 1
    authority.mark_source_revoked(source)
    assert not authority.verify_source(source)


@pytest.mark.asyncio
async def test_cutover_handover_accounts_native_prefix_without_reextraction(capture_case):
    from yeoman_gateway.knowledge._history_cutover import prepare_capture_inputs
    h, k, _, _ = capture_case
    h.add("old", line=1)
    h.add("completed", line=2)
    h.add("job", line=3, chat=GROUP)
    h.add("unknown", line=4, known=False)
    refs = issue(h, k, ("completed", "job"))
    p = producer(k)
    with h.snapshot() as snap:
        with p.scope(snap):
            k.enqueue_capture((refs[1],), context=TrustedCaptureContext(
                "legacy", k.policy_revision, "observed_source_batch", (refs[1],)),
                ts_ms=MS)
        jobs_before = [dict(row) for row in k._store.query("SELECT * FROM knowledge_jobs")]
        q = HistoryQueries(snap)
        rows = [
            {"message_id": "old", "created_ms": 1, "event_id": "a", "boundary": [10, "b"], "forward_start": [5, "start"]},
            {"message_id": "completed", "source": asdict(refs[0]), "completed": True},
            {"message_id": "job", "source": asdict(refs[1])},
            {"message_id": "unknown", "created_ms": 11, "event_id": "z", "boundary": [10, "b"]},
        ]
        inputs = prepare_capture_inputs(queries=q, legacy_boundary=(10, "b"), legacy_rows=rows, jobs=jobs_before)
        receipt = p.prepare_handover(snap, legacy_boundary=(10, "b"), **inputs)
        assert p.prepare_handover(snap, legacy_boundary=(10, "b"), **inputs) == receipt
        assert set(r["message_id"] for r in k._store.query("SELECT * FROM knowledge_history_capture")) == {"old", "completed", "job", "unknown"}
        assert k._store.query_one("SELECT revision FROM knowledge_history_capture WHERE message_id='unknown'")[0] == 0
        assert [dict(row) for row in k._store.query("SELECT * FROM knowledge_jobs")] == jobs_before
        with pytest.raises(ValueError, match="handover_receipt_conflict"):
            p.prepare_handover(snap, pending=(), processed=(), legacy_boundary=(10, "b"),
                               classifications={"old": "pending"})
    seen = []
    w = worker(h, k, p, lambda items: seen.extend(i.event_id for i in items) or [])
    assert seen == []
    await w.run_due(now_ms=MS+100)
    assert "old" not in seen and "completed" not in seen


@pytest.mark.parametrize("assignments,error", [
    ({}, "unclassified_handover_message"),
    ({"old": "typo"}, "unknown_handover_classification"),
    ({"absent": "pending"}, "out_of_prefix_handover_classification"),
    ({"old": "empty_text"}, "classification_refusal_mismatch"),
])
def test_classifications_refuse_invalid_input(capture_case, assignments, error):
    h, k, _, _ = capture_case
    h.add("old", line=1)
    p = producer(k)
    with h.snapshot() as snap, pytest.raises(ValueError, match=error):
        p.prepare_handover(snap, pending=(), processed=(), legacy_boundary=(1, "e"),
                           classifications=assignments)
    assert k._store.query("SELECT * FROM knowledge_history_capture") == []


def test_capture_input_requires_preserved_order_and_rejects_conflicts(capture_case):
    from yeoman_gateway.knowledge._history_cutover import prepare_capture_inputs
    h, _, _, _ = capture_case
    h.add("old", line=1)
    with h.snapshot() as snap:
        q = HistoryQueries(snap)
        with pytest.raises(ValueError, match="unclassified_handover_message"):
            prepare_capture_inputs(queries=q, legacy_boundary=(10, "b"), legacy_rows=[], jobs=[])
        with pytest.raises(ValueError, match="conflicting_capture_proof"):
            prepare_capture_inputs(queries=q, legacy_boundary=(10, "b"), legacy_rows=[
                {"message_id": "old", "created_ms": 1, "event_id": "a", "boundary": [10, "b"], "forward_start": [5, "start"]},
                {"message_id": "old", "created_ms": 11, "event_id": "z", "boundary": [10, "b"]}], jobs=[])


def script():
    path = Path(__file__).resolve().parents[2] / "scripts/prepare_history_cutover.py"
    spec = importlib.util.spec_from_file_location("prepare_cutover", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_operator_refuses_paths_before_access(tmp_path, monkeypatch, capsys):
    module = script()
    args = ["--" + name.replace("_", "-") for name in (
        "snapshot_home", "history_db", "knowledge_source", "knowledge_target", "policy_snapshot", "output_root")]
    paths = [tmp_path / str(i) for i in range(6)]
    paths[0] = Path("/home/dm/.yeoman/data/never-access")
    argv = [item for pair in zip(args, map(str, paths), strict=True) for item in pair]
    monkeypatch.setattr(Path, "read_bytes", lambda _: pytest.fail("read before refusal"))
    monkeypatch.setattr(Path, "mkdir", lambda *a, **kw: pytest.fail("mkdir before refusal"))
    assert module.main(argv) == 1
    assert json.loads(capsys.readouterr().out) == {"ok": False, "error": "unsafe_paths"}
    paths[0] = tmp_path / "link"
    paths[0].symlink_to(tmp_path / "missing")
    argv = [item for pair in zip(args, map(str, paths), strict=True) for item in pair]
    assert module.main(argv) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "unsafe_paths"


def test_operator_prepares_private_manifest_and_handover(tmp_path, capsys):
    from yeoman_gateway.history.attestations import make
    from yeoman_gateway.history.project import project
    from yeoman_gateway.knowledge._store import KnowledgeStore
    from yeoman_shared.raw_archive.records import dumps

    module = script()
    home = tmp_path / "snapshot"
    raw = home / "raw"
    owner = raw / "owner/attestations.jsonl"
    owner.parent.mkdir(parents=True)
    phone = "10001@s.whatsapp.net"
    owner.write_text("\n".join(json.dumps(r) for r in (
        make("contact", 1, "synthetic", identifiers=[phone], name="Synthetic"),
        make("identifier", 1, "synthetic", anchor=phone, identifier=phone, valid_from_ms=1),
    )) + "\n")
    native = raw / "whatsapp/2026-10.jsonl"
    native.parent.mkdir()
    native.write_text(dumps({
        "account": "synthetic", "archive_version": 1, "channel": "whatsapp",
        "chat_id": phone, "direction": "in", "kind": "message", "received_ms": MS,
        "correlation_id": "", "media": None, "native": {"type": "message", "payload": {
            "chatJid": phone, "senderId": phone, "timestamp": MS, "messageId": "M",
            "text": "Synthetic original"}}}) + "\n")
    db = tmp_path / "history.db"
    project([raw], db)
    from yeoman_gateway.history.live import HistoryBoundary
    from yeoman_gateway.history.reader import HistoryReader
    from yeoman_shared.raw_archive.records import enumerate_committed
    reader = HistoryReader(db)
    snap = reader.open_snapshot(HistoryBoundary(1, enumerate_committed(raw)))
    mid = HistoryQueries(snap).native_message(chat_id=phone, native_id="M")["message_id"]
    snap.close()
    reader.close()
    bundle = {"version": 1, "snapshot_digest": "a" * 64, "conversion_digest": "b" * 64,
              "legacy_rows": [], "preserved_rows": [], "capture_rows": [
                  {"message_id": mid, "created_ms": 1, "event_id": "old", "boundary": [10, "b"], "forward_start": [5, "start"]}]}
    (home / "cutover-inputs.json").write_text(json.dumps(bundle))
    source = tmp_path / "v2.db"
    store = KnowledgeStore(source)
    store.set_meta("migration_complete", "1")
    store.set_meta("statement_capture_boundary_ms", "10")
    store.set_meta("statement_capture_boundary_event_id", "b")
    store.close()
    target = tmp_path / "v3.db"
    output = tmp_path / "output"
    policy = tmp_path / "policy-copy.json"
    policy.write_text(json.dumps({"defaults": {"whoCanTalk": {"mode": "everyone"}}}))
    values = (home, db, source, target, policy, output)
    argv = [item for name, path in zip(module._ARGUMENTS, values, strict=True)
            for item in ("--" + name.replace("_", "-"), str(path))]
    assert module.main(argv) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["ok"] and summary["handover"] and summary["total"] == 0
    assert set(summary) == {"ok", "handover", *module._COUNTS}
    manifest_path = output / "legacy-alias-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    assert manifest["version"] == 1 and manifest["entries"] == []
    digest = manifest.pop("digest")
    assert row_sha256(manifest) == digest
    assert manifest["handover"]["classifications"] == {mid: "historical_not_selected"}
    assert manifest_path.stat().st_mode & 0o777 == 0o600
    assert output.stat().st_mode & 0o777 == 0o700
    assert "Synthetic original" not in manifest_path.read_text()


def test_current_progress_cursor_alone_does_not_prove_historical_exclusion(capture_case):
    from yeoman_gateway.knowledge._history_cutover import prepare_capture_inputs
    h, _, _, _ = capture_case
    h.add("old", line=1)
    with h.snapshot() as snap, pytest.raises(ValueError, match="unclassified_handover_message"):
        prepare_capture_inputs(queries=HistoryQueries(snap), legacy_boundary=(10, "b"),
            legacy_rows=[{"message_id": "old", "created_ms": 1, "event_id": "a",
                          "boundary": [10, "b"]}], jobs=[])


def test_operator_argument_errors_never_echo_values(capsys):
    module = script()
    assert module.main(["--unexpected", "synthetic-private-value"]) == 1
    output = capsys.readouterr()
    assert json.loads(output.out) == {"ok": False, "error": "invalid_arguments"}
    assert output.err == ""


def test_missing_audience_proof_is_withheld(case):
    from yeoman_gateway.knowledge._history_cutover import prepare_legacy_alias_inputs
    _, q, _, _ = case
    source = SourceRef("legacy", 3, "whatsapp", q.message("m")["chat_id"], "whatsapp:10001", 100)
    row = legacy(source)
    del row["source_audience_json"]
    rows, locators, report = prepare_legacy_alias_inputs(
        queries=q, legacy_rows=[row], preserved_rows=[proof(q, "m", source)])
    assert report["missing"] == 1 and locators == {}
    assert rows[0]["cutover_status"] == "missing"


def test_nested_symlink_is_refused_before_mkdir(tmp_path, monkeypatch, capsys):
    module = script()
    home = tmp_path / "snapshot"
    raw = home / "raw"
    raw.mkdir(parents=True)
    (raw / "nested").symlink_to(tmp_path / "unread", target_is_directory=True)
    values = (home, tmp_path / "history.db", tmp_path / "source.db", tmp_path / "target.db",
              tmp_path / "policy-copy.json", tmp_path / "out")
    argv = [item for name, path in zip(module._ARGUMENTS, values, strict=True)
            for item in ("--" + name.replace("_", "-"), str(path))]
    monkeypatch.setattr(Path, "read_bytes", lambda _: pytest.fail("read before refusal"))
    monkeypatch.setattr(Path, "mkdir", lambda *a, **kw: pytest.fail("mkdir before refusal"))
    assert module.main(argv) == 1
    assert json.loads(capsys.readouterr().out) == {"ok": False, "error": "preparation_failed"}


def test_classification_and_proven_source_cannot_overlap(capture_case):
    h, k, _, _ = capture_case
    h.add("source", line=1)
    source = issue(h, k, ("source",))[0]
    p = producer(k)
    with h.snapshot() as snap, pytest.raises(ValueError, match="ambiguous_handover_source"):
        p.prepare_handover(snap, pending=(source,), processed=(), legacy_boundary=(10, "b"),
                           classifications={"source": "historical_not_selected"})
    assert k._store.query("SELECT * FROM knowledge_history_capture") == []


def test_other_channel_jobs_have_no_whatsapp_assignment(capture_case):
    from yeoman_gateway.knowledge._history_cutover import prepare_capture_inputs
    h, k, _, _ = capture_case
    from yeoman_gateway.knowledge.authority import EvidenceAudience
    source = SourceRef("telegram-old", 1, "telegram", "synthetic", "telegram:10001", 100)
    k._legacy_authority.register_source(source, EvidenceAudience.author_only())
    p = producer(k)
    with h.snapshot() as snap, p.scope(snap):
        k.enqueue_capture((source,), context=TrustedCaptureContext(
            "legacy", k.policy_revision, "observed_source_batch", (source,)), ts_ms=MS)
    jobs = [dict(row) for row in k._store.query("SELECT * FROM knowledge_jobs")]
    with h.snapshot() as snap:
        inputs = prepare_capture_inputs(queries=HistoryQueries(snap), legacy_boundary=(10, "b"),
            legacy_rows=[{**legacy(source), "cutover_status": "other_channel"}],
            jobs=jobs)
        assert inputs == {"pending": (), "processed": (), "classifications": {}}
        p.prepare_handover(snap, legacy_boundary=(10, "b"), **inputs)
    assert [dict(row) for row in k._store.query("SELECT * FROM knowledge_jobs")] == jobs


@pytest.mark.parametrize(('bad','reason'),[
    ({'author_principal':''},'unissued_principal'),
    ({'author_principal':'   '},'unissued_principal'),
    ({'author_principal':'bad\x00principal'},'invalid_source_ref'),
    ({'author_principal':None},'invalid_source_ref'),
    ({'occurred_at_ms':-1},'invalid_source_ref'),
    ({'channel':''},'invalid_source_ref'),
    ({'revision':0},'invalid_source_ref'),
])
def test_unissuable_legacy_source_is_missing_per_key(case,bad,reason):
    from yeoman_gateway.knowledge._history_cutover import prepare_legacy_alias_inputs
    _,q,_,db = case
    source = SourceRef("unissued",1,"whatsapp",q.message("m")["chat_id"],"whatsapp:10001",100)
    row = legacy(source,**bad)
    rows,locators,counts = prepare_legacy_alias_inputs(queries=q,legacy_rows=[row,row],preserved_rows=[])
    assert counts['total']==counts['missing']==1
    assert counts['mapped']==0 and locators=={}
    assert rows[0]['cutover_status']=='missing' and rows[0]['cutover_reason']==reason
    assert rows[0]['author_principal']==bad.get('author_principal',source.author_principal)


@pytest.mark.asyncio
async def test_unissued_source_pending_zero_does_not_rescue_active_job(capture_case):
    from yeoman_gateway.knowledge._history_cutover import prepare_capture_inputs
    h,k,_,_ = capture_case
    h.add('unissued',line=1,known=False)
    p = producer(k)
    rows = [dict(message_id='unissued',event_id='unissued',revision=1,author_principal='',
        channel='whatsapp',cutover_status='missing',created_ms=11,boundary=[10,'b'])]
    with h.snapshot() as snap:
        q = HistoryQueries(snap)
        job = dict(state='queued',sources_json=json.dumps([dict(event_id='unissued',revision=1,channel='whatsapp')]))
        from scripts.prepare_history_cutover import _affected_counts
        assert _affected_counts(rows,[dict(statement_id='withheld',event_id='unissued',revision=1)],[job],{}) == dict(statements=1,jobs=1,withheld_statements=1,affected_jobs=1)
        with pytest.raises(ValueError,match='unmapped_handover_job'):
            prepare_capture_inputs(queries=q,legacy_boundary=(10,'b'),legacy_rows=rows,jobs=[job])
        inputs = prepare_capture_inputs(queries=q,legacy_boundary=(10,'b'),legacy_rows=rows,jobs=[])
        assert inputs['classifications']=={'unissued':'pending'}
        p.prepare_handover(snap,legacy_boundary=(10,'b'),**inputs)
    row = k._store.query_one("SELECT revision,outcome FROM knowledge_history_capture WHERE message_id='unissued'")
    assert tuple(row)==(0,'pending')
