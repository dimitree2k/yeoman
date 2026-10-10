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
    assert prepare_legacy_alias_inputs(queries=q, legacy_rows=[legacy(source)], preserved_rows=[missing])[2]["mapped"] == 1
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
            prepare_capture_inputs(queries=q, legacy_boundary=(10, "b"), legacy_rows=[{"message_id": "old"}], jobs=[])
        with pytest.raises(ValueError, match="conflicting_capture_proof"):
            prepare_capture_inputs(queries=q, legacy_boundary=(10, "b"), legacy_rows=[
                {"message_id": "old", "created_ms": 1, "event_id": "a", "boundary": [10, "b"], "forward_start": [5, "start"]},
                {"message_id": "old", "created_ms": 11, "event_id": "z", "boundary": [10, "b"], "forward_start": [7, "start"]}], jobs=[])


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


@pytest.mark.parametrize("duplicate",[False,True])
def test_operator_prepares_private_manifest_and_handover(tmp_path, capsys,duplicate):
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
    if duplicate:
        bundle["capture_rows"].append(dict(bundle["capture_rows"][0],event_id="redelivery",created_ms=11))
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
    assert summary["duplicate_observations"]==int(duplicate)
    assert summary["duplicate_mapped_sources"]==summary["historical_backfill_aliases"]==0
    assert set(summary) == {"ok", "handover", *module._COUNTS}
    manifest_path = output / "legacy-alias-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    assert manifest["version"] == 1 and manifest["entries"] == []
    assert manifest["capture_summary"]["duplicate_observations"]==int(duplicate)
    assert manifest["capture_summary"]["duplicate_mapped_sources"]==manifest["capture_summary"]["historical_backfill_aliases"]==0
    if duplicate:
        assert manifest["capture_summary"]["observations"][mid]==[dict(created_ms=1,event_id="old"),dict(created_ms=11,event_id="redelivery")]
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


@pytest.mark.parametrize(('early','completed','outcome'),[(4,False,'historical_not_selected'),(6,False,'pending'),(6,True,'processed')])
@pytest.mark.parametrize('reverse',[False,True])
def test_duplicate_observation_uses_earliest_order(capture_case,early,completed,outcome,reverse):
    from yeoman_gateway.knowledge._history_cutover import prepare_capture_inputs
    h,k,_,_ = capture_case
    h.add('duplicate',line=1)
    source = issue(h,k,('duplicate',))[0]
    observations = [dict(message_id='duplicate',event_id=event,created_ms=created,source=asdict(source),
        completed=completed,boundary=[10,'b'],forward_start=[5,'start']) for event,created in (('early',early),('late',12))]
    observations.append(dict(observations[0]))
    if reverse:
        observations.reverse()
    summary = {}
    with h.snapshot() as snap:
        inputs = prepare_capture_inputs(queries=HistoryQueries(snap),legacy_boundary=(10,'b'),legacy_rows=observations,jobs=[],summary=summary)
        assert summary['duplicate_observations']==1
        assert summary['observations']['duplicate']==[dict(created_ms=early,event_id='early'),dict(created_ms=12,event_id='late')]
        if outcome=='historical_not_selected':
            assert inputs['classifications']=={'duplicate':outcome} and inputs['pending']==inputs['processed']==()
        else:
            assert inputs[outcome]==(source,) and inputs['classifications']=={}
        producer(k).prepare_handover(snap,legacy_boundary=(10,'b'),**inputs)


@pytest.mark.parametrize('field',['completed','status','source','author_principal','source_audience_json','boundary','forward_start'])
def test_duplicate_observation_still_refuses_other_conflicts(capture_case,field):
    from yeoman_gateway.knowledge._history_cutover import prepare_capture_inputs
    h,k,_,_ = capture_case
    h.add('duplicate',line=1)
    source = issue(h,k,('duplicate',))[0]
    first = dict(message_id='duplicate',event_id='early',created_ms=6,source=asdict(source),completed=False,
        status='active',author_principal=source.author_principal,source_audience_json=None,boundary=[10,'b'],forward_start=[5,'start'])
    second = dict(first,event_id='late',created_ms=12)
    second[field] = {'completed':True,'status':'revoked','source':asdict(replace(source,event_id='different')),
        'author_principal':'whatsapp:10002','source_audience_json':'[]','boundary':[11,'b'],'forward_start':[7,'start']}[field]
    with h.snapshot() as snap,pytest.raises(ValueError,match='conflicting_capture_proof'):
        prepare_capture_inputs(queries=HistoryQueries(snap),legacy_boundary=(10,'b'),legacy_rows=[first,second],jobs=[])


def test_duplicate_observation_cannot_reassign_source_identity(capture_case):
    from yeoman_gateway.knowledge._history_cutover import prepare_capture_inputs
    h,k,_,_ = capture_case
    h.add('duplicate',line=1)
    source = issue(h,k,('duplicate',))[0]
    first = dict(asdict(source),message_id='duplicate',cutover_status='mapped',created_ms=6,
        boundary=[10,'b'],forward_start=[5,'start'])
    with h.snapshot() as snap:
        q = HistoryQueries(snap)
        summary = {}
        inputs = prepare_capture_inputs(queries=q,legacy_boundary=(10,'b'),legacy_rows=[first,dict(first,created_ms=12)],jobs=[],summary=summary)
        assert inputs['pending']==(source,) and summary['duplicate_observations']==1
        with pytest.raises(ValueError,match='conflicting_capture_proof'):
            prepare_capture_inputs(queries=q,legacy_boundary=(10,'b'),legacy_rows=[first,dict(first,event_id='other',created_ms=12)],jobs=[])
        with pytest.raises(ValueError,match='conflicting_capture_proof'):
            prepare_capture_inputs(queries=q,legacy_boundary=(10,'b'),legacy_rows=[first,dict(first,message_id='other')],jobs=[])


def test_duplicate_observation_tie_breaks_by_event_id(capture_case):
    from yeoman_gateway.knowledge._history_cutover import prepare_capture_inputs
    h,_,_,_ = capture_case
    h.add('duplicate',line=1,known=False)
    rows = [dict(message_id='duplicate',created_ms=5,event_id=event,boundary=[10,'b'],forward_start=[5,'m']) for event in ('z','a')]
    summary = {}
    with h.snapshot() as snap:
        result = prepare_capture_inputs(queries=HistoryQueries(snap),legacy_boundary=(10,'b'),legacy_rows=rows,jobs=[],summary=summary)
    assert result['classifications']=={'duplicate':'historical_not_selected'}
    assert summary['observations']['duplicate'][0]==dict(created_ms=5,event_id='a')


@pytest.mark.parametrize('edited',[False,True])
def test_recorded_partial_state_maps_and_records_proven_fields(case,edited):
    from yeoman_gateway.knowledge._history_cutover import prepare_legacy_alias_inputs
    _,q,_,db = case
    source = SourceRef('partial',1,'whatsapp',q.message('m')['chat_id'],'whatsapp:10001',100)
    original = proof(q,'m',source)
    for field in ('media_json','mentions_json','current_text','reply_to_native_id','events'):
        original['original']['state'].pop(field)
    original['origin']['row_sha256'] = row_sha256(original['original'])
    if edited:
        event(db,'edit-after','edit',200,{'text':'Edited synthetic'},target='m')
    rows,locators,counts = prepare_legacy_alias_inputs(queries=q,legacy_rows=[legacy(source)],preserved_rows=[original])
    assert counts['mapped']==1 and locators[source.key]==('m',)
    assert 'text' in rows[0]['proven_fields'] and 'media_json' not in rows[0]['proven_fields']
    bad = copy.deepcopy(original)
    bad['original']['state']['mentions_json'] = ['different']
    bad['origin']['row_sha256'] = row_sha256(bad['original'])
    rows,_,counts = prepare_legacy_alias_inputs(queries=q,legacy_rows=[legacy(source)],preserved_rows=[bad])
    assert counts['changed']==1 and rows[0]['cutover_reason']=='recorded_field_mismatch'


def test_compatible_partial_copies_collapse_and_missing_reasons(case):
    from yeoman_gateway.knowledge._history_cutover import prepare_legacy_alias_inputs
    _,q,_,_ = case
    source = SourceRef('partial',1,'whatsapp',q.message('m')['chat_id'],'whatsapp:10001',100)
    first = proof(q,'m',source)
    second = copy.deepcopy(first)
    second['original']['state'].pop('media_json')
    second['origin']['row_sha256'] = row_sha256(second['original'])
    rows,_,counts = prepare_legacy_alias_inputs(queries=q,legacy_rows=[legacy(source)],preserved_rows=[first,second])
    assert counts['mapped']==1
    second['original'].pop('issued')
    second['original']['state'].pop('text')
    second['origin']['row_sha256'] = row_sha256(second['original'])
    rows,_,counts = prepare_legacy_alias_inputs(queries=q,legacy_rows=[legacy(source)],preserved_rows=[first,second])
    assert counts['mapped']==1
    rows,_,_ = prepare_legacy_alias_inputs(queries=q,legacy_rows=[legacy(source)],preserved_rows=[])
    assert rows[0]['cutover_reason']=='no_preserved_original'
    row = legacy(source)
    row.pop('source_audience_json')
    rows,_,_ = prepare_legacy_alias_inputs(queries=q,legacy_rows=[row],preserved_rows=[first])
    assert rows[0]['cutover_reason']=='no_audience_proof'


@pytest.mark.parametrize(('state','reason','active'),[
    ('done',None,False),('cancelled',None,False),('skipped','policy',False),
    ('queued',None,True),('running',None,True),('failed',None,True),('skipped','queue_full',True)])
def test_only_active_unmapped_jobs_block_both_handover_paths(capture_case,state,reason,active):
    from yeoman_gateway.knowledge._history_cutover import prepare_capture_inputs
    h,k,_,_ = capture_case
    h.add('unmapped',line=1)
    mapped, = issue(h,k,('unmapped',))
    p = producer(k)
    source = SourceRef('unmapped-old',1,'whatsapp',GROUP,'whatsapp:10001',MS)
    with h.snapshot() as snap, p.scope(snap):
        k.enqueue_capture((mapped,),context=TrustedCaptureContext('legacy',k.policy_revision,'observed_source_batch',(mapped,)),ts_ms=MS)
    k._store.execute('UPDATE knowledge_jobs SET state=?,reason=?,sources_json=?',(state,reason,json.dumps([asdict(source)])))
    jobs = [dict(r) for r in k._store.query('SELECT * FROM knowledge_jobs')]
    rows = [dict(message_id='unmapped',event_id='unmapped-old',created_ms=1,boundary=[10,'b'],forward_start=[5,'start'])]
    with h.snapshot() as snap:
        if active:
            with pytest.raises(ValueError,match='unmapped_handover_job'):
                prepare_capture_inputs(queries=HistoryQueries(snap),legacy_boundary=(10,'b'),legacy_rows=rows,jobs=jobs)
            with pytest.raises(ValueError,match='unmapped_handover_job'):
                p.prepare_handover(snap,pending=(),processed=(),legacy_boundary=(10,'b'),classifications={'unmapped':'historical_not_selected'})
        else:
            summary = {}
            inputs = prepare_capture_inputs(queries=HistoryQueries(snap),legacy_boundary=(10,'b'),legacy_rows=rows,jobs=jobs,summary=summary)
            assert summary['unmapped_terminal_job_refs']==1
            receipt = p.prepare_handover(snap,legacy_boundary=(10,'b'),**inputs)
            assert receipt['unmapped_terminal_job_refs']==1
            assert p.prepare_handover(snap,legacy_boundary=(10,'b'),**inputs)==receipt
            assert [dict(r) for r in k._store.query('SELECT * FROM knowledge_jobs')]==jobs


@pytest.mark.parametrize('conflict', [False, True])
def test_cross_store_union_of_recorded_fields(case, conflict):
    from yeoman_gateway.knowledge._history_cutover import prepare_legacy_alias_inputs
    _, q, _, _ = case
    source = SourceRef('union', 1, 'whatsapp', q.message('m')['chat_id'], 'whatsapp:10001', 100)
    first, second = proof(q, 'm', source), proof(q, 'm', source)
    full = first['original']['state']
    first['original']['state'] = {k: full[k] for k in ('channel', 'chat_id', 'native_message_id', 'text')}
    second['original']['state'] = {k: full[k] for k in ('sent_ms', 'time_certainty')}
    second['origin']['store'] = 'memory'
    if conflict:
        second['original']['state']['text'] = 'Conflicting preserved text'
    for item in (first, second):
        item['origin']['row_sha256'] = row_sha256(item['original'])
    rows, locators, counts = prepare_legacy_alias_inputs(queries=q, legacy_rows=[legacy(source)], preserved_rows=[first, second])
    assert rows[0]['cutover_status'] == ('ambiguous' if conflict else 'mapped')
    assert counts['ambiguous' if conflict else 'mapped'] == 1
    if not conflict:
        assert locators[source.key] == ('m',)
        assert rows[0]['proven_fields'] == sorted(['channel', 'chat_id', 'native_message_id', 'text', 'sent_ms', 'time_certainty', 'issued', 'original_row_sha256'])
    assert prepare_legacy_alias_inputs(queries=q, legacy_rows=[legacy(source)], preserved_rows=[second, first]) == (rows, locators, counts)


def test_legacy_node_keeps_legacy_authority(case, monkeypatch):
    from yeoman_gateway.knowledge._history_cutover import prepare_legacy_alias_inputs
    service, q, authority, _ = case
    store = service._store
    with store.transaction():
        store.execute("UPDATE knowledge_statement_sources SET event_id='legacy-node:synthetic',revision=1,source_audience_json=NULL WHERE event_id='curated-source'")
    original = dict(store.query_one("SELECT * FROM knowledge_statement_sources WHERE event_id='legacy-node:synthetic'"))
    rows, locators, counts = prepare_legacy_alias_inputs(queries=q, legacy_rows=[original], preserved_rows=[])
    assert counts['legacy_node'] == 1 and counts['missing'] == 0
    assert rows[0]['cutover_status'] == 'legacy_node' and not locators
    # Even an unavailable history projection cannot become authority for a note.
    monkeypatch.setattr(q.snapshot, 'assert_current', lambda *a: pytest.fail('note consulted history'))
    note = authority.verify_source_ref(original['event_id'], original['revision'])
    assert note == SourceRef(**{k: original[k] for k in SourceRef.__dataclass_fields__})
    assert authority.verify_source(note)
    assert authority.permits_principal(note, note.author_principal, now_ms=100)
    assert service._statements.sources_of(original['statement_id'])[0][0] == note
    assert authority.register_source(note, authority.evidence_audience(note, basis='')) == note
    with store.transaction():
        store.execute("UPDATE knowledge_statement_sources SET source_audience_json='[]' WHERE event_id=?", (note.event_id,))
    assert not authority.permits_principal(note, note.author_principal, now_ms=100)
    authority.mark_source_revoked(note)
    assert authority.verify_source_ref(*note.key) is None


def test_no_legacy_row_is_pending_zero(capture_case):
    from yeoman_gateway.knowledge._history_cutover import prepare_capture_inputs
    h, k, _, _ = capture_case
    h.add('never-observed', line=1, known=False)
    h.add('empty-refusal', line=2)
    import sqlite3
    with sqlite3.connect(h.path) as db:
        db.execute("UPDATE messages SET text='' WHERE message_id='empty-refusal'")
    p = producer(k)
    with h.snapshot() as snap:
        summary = {}
        inputs = prepare_capture_inputs(queries=HistoryQueries(snap), legacy_boundary=(10, 'b'), legacy_rows=[], jobs=[], summary=summary)
        assert inputs == {'pending': (), 'processed': (), 'classifications': {'empty-refusal': 'empty_text', 'never-observed': 'pending'}}
        assert summary['no_legacy_row_pending'] == 1
        p.prepare_handover(snap, legacy_boundary=(10, 'b'), **inputs)
        row = k._store.query_one("SELECT * FROM knowledge_history_capture WHERE message_id='never-observed'")
        assert row['revision'] == 0 and row['outcome'] == 'pending'
        with pytest.raises(ValueError, match='unclassified_handover_message'):
            prepare_capture_inputs(queries=HistoryQueries(snap), legacy_boundary=(10, 'b'), legacy_rows=[{'message_id': 'never-observed'}], jobs=[])


def test_legacy_node_cannot_be_aliased_even_with_a_native_locator(case):
    _, q, _, _ = case
    note = SourceRef('legacy-node:synthetic', 1, 'whatsapp', q.message('m')['chat_id'], 'whatsapp:10001', 100)
    row = legacy(note, content_fingerprint=q.content_fingerprint('m'))
    aliases, counts = build_history_source_aliases(queries=q, legacy_rows=[row], locators={note.key: ('m',)})
    assert not aliases and counts['legacy_node']==1


@pytest.mark.parametrize(('left_author','right_author','expected'), [
    ('whatsapp:10001','10001@s.whatsapp.net','mapped'),
    ('+10001','whatsapp:10001','mapped'),
    ('whatsapp:10001','10002@s.whatsapp.net','ambiguous'),
    ('unknown-a','unknown-b','ambiguous'),
])
def test_cross_store_author_comparison_is_canonical(case, left_author, right_author, expected):
    from yeoman_gateway.knowledge._history_cutover import prepare_legacy_alias_inputs
    _,q,_,_ = case
    source = SourceRef('authors',1,'whatsapp',q.message('m')['chat_id'],'whatsapp:10001',100)
    copies = [proof(q,'m',source),proof(q,'m',source)]
    for item,author in zip(copies,(left_author,right_author),strict=True):
        item['original']['state']['author_principal'] = author
        item['original']['issued']['author_principal'] = author
        item['origin']['row_sha256'] = row_sha256(item['original'])
    rows,_,counts = prepare_legacy_alias_inputs(queries=q,legacy_rows=[legacy(source)],preserved_rows=copies)
    assert rows[0]['cutover_status']==expected and counts[expected]==1
    assert copies[1]['original']['state']['author_principal']==right_author


@pytest.mark.parametrize(('first_reply','second_reply','text_conflict','expected'), [
    (None,'absent',False,'mapped'),('', 'absent',False,'mapped'),
    (None,'parent',False,'mapped'),('', 'parent',False,'mapped'),
    ('parent','different',False,'ambiguous'),(None,'absent',True,'ambiguous'),
])
def test_cross_store_reply_absence_and_strict_text(case, first_reply, second_reply, text_conflict, expected):
    from yeoman_gateway.knowledge._history_cutover import prepare_legacy_alias_inputs
    _,q,_,db = case
    if second_reply=='parent':
        db.execute("UPDATE messages SET reply_to_native_id='parent' WHERE message_id='m'")
    source = SourceRef('replies',1,'whatsapp',q.message('m')['chat_id'],'whatsapp:10001',100)
    copies = [proof(q,'m',source),proof(q,'m',source)]
    copies[0]['original']['state']['reply_to_native_id'] = first_reply
    if second_reply=='absent':
        copies[1]['original']['state'].pop('reply_to_native_id')
    else:
        copies[1]['original']['state']['reply_to_native_id'] = second_reply
    if text_conflict:
        copies[1]['original']['state']['text'] += ' '
    for item in copies:
        item['origin']['row_sha256'] = row_sha256(item['original'])
    result = prepare_legacy_alias_inputs(queries=q,legacy_rows=[legacy(source)],preserved_rows=copies)
    assert result[0][0]['cutover_status']==expected and result[2][expected]==1
    if expected=='mapped':
        assert ('reply_to_native_id' in result[0][0]['proven_fields'])==(second_reply=='parent')
    assert prepare_legacy_alias_inputs(queries=q,legacy_rows=[legacy(source)],preserved_rows=list(reversed(copies)))==result


@pytest.mark.parametrize('direction',['in','out'])
def test_cutover_uses_loaded_producer_policy_precedence(capture_case, direction):
    from yeoman_gateway.knowledge._history_cutover import prepare_capture_inputs
    h,k,_,_ = capture_case
    h.add('denied-chat',line=1,chat='not-configured@g.us',direction=direction)
    p = producer(k)
    with h.snapshot() as snap:
        inputs = prepare_capture_inputs(queries=HistoryQueries(snap),legacy_boundary=(10,'b'),
            legacy_rows=[],jobs=[],permanent_reason=p._permanent_reason)
        assert inputs['classifications']=={'denied-chat':'not_policy_chat'}
        assert inputs['pending']==inputs['processed']==()
        receipt = p.prepare_handover(snap,legacy_boundary=(10,'b'),**inputs)
        assert receipt['classifications']==inputs['classifications']


def test_canonical_author_comparison_retains_raw_issued_ref(case):
    from yeoman_gateway.knowledge._history_cutover import prepare_legacy_alias_inputs
    _,q,authority,_ = case
    source = SourceRef('raw-issued',1,'whatsapp',q.message('m')['chat_id'],'10001@s.whatsapp.net',100)
    original = proof(q,'m',source)
    rows,locators,counts = prepare_legacy_alias_inputs(queries=q,legacy_rows=[legacy(source)],preserved_rows=[original])
    assert counts['mapped']==1 and rows[0]['author_principal']==source.author_principal
    aliases,_ = build_history_source_aliases(queries=q,legacy_rows=rows,locators=locators)
    assert aliases[source.key].issued==source
    authority.ledger.persist_aliases(aliases)
    assert authority.verify_source_ref(*source.key)==source
    assert authority.permits_principal(source,'whatsapp:10001',now_ms=101)
    assert original['original']['issued']['author_principal']==source.author_principal


@pytest.mark.parametrize(('contact','start','expected','reason'), [
    ('a',1,'mapped','mapped'),
    ('b',1,'ambiguous','author_different_contact'),
    ('a',101,'ambiguous','author_unresolved'),
])
def test_cross_store_author_identity_at_message_time(case, contact, start, expected, reason):
    from yeoman_gateway.knowledge._history_cutover import prepare_legacy_alias_inputs

    from tests.gateway.convhist.test_hist_queries import identifier
    _,q,sources,db = case
    identifier(db,contact,'90001@lid',start=start)
    source = SourceRef('identity',1,'whatsapp',q.message('m')['chat_id'],'whatsapp:90001@lid',100)
    left,right = proof(q,'m',source),proof(q,'m',source)
    for item,author in ((left,'whatsapp:90001@lid'),(right,'10001@s.whatsapp.net')):
        item['original']['state']['author_principal'] = author
        item['original']['issued']['author_principal'] = author
        item['origin']['row_sha256'] = row_sha256(item['original'])
    rows,locators,counts = prepare_legacy_alias_inputs(queries=q,legacy_rows=[legacy(source)],preserved_rows=[left,right])
    assert rows[0]['cutover_status']==expected
    assert rows[0].get('author_reason',rows[0]['cutover_reason'])==reason
    assert counts[expected]==1
    if expected=='mapped':
        aliases,_ = build_history_source_aliases(queries=q,legacy_rows=rows,locators=locators)
        assert aliases[source.key].issued==source
        sources.ledger.persist_aliases(aliases)
        assert sources.verify_source_ref(*source.key)==source
        assert sources.permits_principal(source,'whatsapp:10001',now_ms=100)
        assert not sources.permits_principal(source,'whatsapp:10002',now_ms=100)
    else:
        assert counts[reason]==1


def test_identical_unresolved_authors_are_withheld_with_identity_reason(case):
    from yeoman_gateway.knowledge._history_cutover import prepare_legacy_alias_inputs
    _,q,_,db = case
    source = SourceRef('unresolved',1,'whatsapp',q.message('m')['chat_id'],'whatsapp:10001',100)
    item = proof(q,'m',source)
    db.execute("DELETE FROM identifier_history WHERE contact_id='a'")
    rows,_,counts = prepare_legacy_alias_inputs(queries=q,legacy_rows=[legacy(source)],preserved_rows=[item])
    assert rows[0]['cutover_status']=='changed'
    assert rows[0]['cutover_reason']=='author_mismatch'
    assert rows[0]['author_reason']=='author_unresolved'
    assert counts['author_unresolved']==1 and counts['mapped']==0


@pytest.mark.parametrize(('bare','lid_contact','expected','reason'),[
    ('10001',None,'mapped','mapped'),
    ('90001','a','mapped','mapped'),
    ('10001','a','mapped','mapped'),
    ('10001','b','ambiguous','author_unresolved'),
    ('90001','b','ambiguous','author_different_contact'),
    ('90001',None,'ambiguous','author_unresolved'),
])
def test_bare_digit_authors_resolve_phone_and_lid_without_rewriting(case,bare,lid_contact,expected,reason):
    from yeoman_gateway.knowledge._history_cutover import prepare_legacy_alias_inputs

    from tests.gateway.convhist.test_hist_queries import identifier
    _,q,sources,db = case
    if lid_contact:
        identifier(db,lid_contact,bare+'@lid',start=1)
    source = SourceRef('bare-author',1,'whatsapp',q.message('m')['chat_id'],bare,100)
    copies = [proof(q,'m',source),proof(q,'m',source)]
    for item,author in zip(copies,(bare,'whatsapp:10001'),strict=True):
        item['original']['state']['author_principal'] = author
        item['original']['issued']['author_principal'] = author
        item['origin']['row_sha256'] = row_sha256(item['original'])
    rows,locators,counts = prepare_legacy_alias_inputs(queries=q,legacy_rows=[legacy(source)],preserved_rows=copies)
    assert rows[0]['cutover_status']==expected
    assert rows[0].get('author_reason',rows[0]['cutover_reason'])==reason
    assert copies[0]['original']['issued']['author_principal']==bare
    if expected=='mapped':
        aliases,_ = build_history_source_aliases(queries=q,legacy_rows=rows,locators=locators)
        assert aliases[source.key].issued==source
        sources.ledger.persist_aliases(aliases)
        assert sources.verify_source_ref(*source.key)==source
        assert sources.permits_principal(source,'whatsapp:10001',now_ms=100)
        assert not sources.permits_principal(source,'whatsapp:10002',now_ms=100)
    else:
        assert counts[reason]==1 and counts['mapped']==0


@pytest.mark.parametrize('contact',['a','b'])
@pytest.mark.parametrize('bare',['10001','90001'])
@pytest.mark.parametrize('integer_original',[False,True])
def test_real_shape_journal_reply_bridge_author_union(case,bare,integer_original,contact):
    from yeoman_gateway.knowledge._history_cutover import prepare_legacy_alias_inputs

    from tests.gateway.convhist.test_hist_queries import identifier
    _,q,sources,db = case
    identifier(db,contact,'90001@lid',start=1)
    source = SourceRef('three-store',1,'whatsapp',q.message('m')['chat_id'],bare,100)
    copies = [proof(q,'m',source) for _ in range(3)]
    journal_author = int(bare) if integer_original else bare
    for item,store,author in zip(copies,('journal','reply_context','bridge_refs'),
            (journal_author,'whatsapp:10001','whatsapp:90001@lid'),strict=True):
        item['original']['state']['author_principal'] = author
        item['original']['issued']['author_principal'] = author
        item['origin'].update(store=store,row_sha256=row_sha256(item['original']))
    rows,locators,counts = prepare_legacy_alias_inputs(queries=q,legacy_rows=[legacy(source)],preserved_rows=copies)
    if contact=='b':
        assert rows[0]['cutover_status']=='ambiguous'
        assert rows[0]['author_reason']=='author_different_contact'
        assert counts['author_different_contact']==1 and counts['mapped']==0
        return
    assert rows[0]['cutover_status']=='mapped' and counts['mapped']==1
    aliases,_ = build_history_source_aliases(queries=q,legacy_rows=rows,locators=locators)
    sources.ledger.persist_aliases(aliases)
    assert sources.verify_source_ref(*source.key)==source
    assert aliases[source.key].issued.author_principal==bare
    assert copies[0]['original']['state']['author_principal']==journal_author


def test_author_resolution_uses_message_clock_not_issued_clock(case):
    from yeoman_gateway.knowledge._history_cutover import prepare_legacy_alias_inputs

    from tests.gateway.convhist.test_hist_queries import identifier
    _,q,_,db = case
    identifier(db,'a','90001@lid',start=1,end=101)
    source = SourceRef('clock',1,'whatsapp',q.message('m')['chat_id'],'90001',101)
    copies = [proof(q,'m',source),proof(q,'m',source)]
    for item,value in zip(copies,('90001','whatsapp:10001'),strict=True):
        item['original']['state']['author_principal'] = value
        item['origin']['row_sha256'] = row_sha256(item['original'])
    rows,_,counts = prepare_legacy_alias_inputs(queries=q,legacy_rows=[legacy(source)],preserved_rows=copies)
    assert rows[0]['cutover_reason']=='time_mismatch'
    assert counts['mapped']==0 and counts['author_unresolved']==0


@pytest.mark.parametrize(('audience','reason'),[('different','audience_mismatch'),('malformed','audience_unproven'),('absent','audience_unproven')])
def test_alias_audience_refusals_have_specific_reason(case,audience,reason):
    from yeoman_gateway.knowledge._history_cutover import prepare_legacy_alias_inputs
    _,q,_,_ = case
    source = SourceRef('audience-refusal',1,'whatsapp',q.message('m')['chat_id'],'10001',100)
    row = legacy(source,content_fingerprint=q.content_fingerprint('m'))
    if audience=='different':
        row['source_audience_json']='["whatsapp:10003"]'
    elif audience=='malformed':
        row['source_audience_json']='not-json'
    else:
        row.pop('source_audience_json')
    aliases,counts = build_history_source_aliases(queries=q,legacy_rows=[row],locators={source.key:('m',)})
    assert not aliases and counts[reason]==1
    if audience!='absent':
        rows,_,_ = prepare_legacy_alias_inputs(queries=q,legacy_rows=[row],preserved_rows=[proof(q,'m',source)])
        assert rows[0]['cutover_reason']==reason


@pytest.mark.parametrize('unknown',[False,True])
@pytest.mark.parametrize('members',[None,['whatsapp:10001','whatsapp:10002','whatsapp:10003']])
def test_alias_audience_ceiling_and_legacy_basis(case,unknown,members):
    from tests.gateway.convhist.test_hist_queries import contact, identifier
    _,q,sources,db = case
    if unknown:
        db.execute("DELETE FROM message_events WHERE kind='member_snapshot'")
    source = SourceRef('audience-ceiling',1,'whatsapp',q.message('m')['chat_id'],'10001',100)
    row = legacy(source,content_fingerprint=q.content_fingerprint('m'),
        source_audience_json=None if members is None else json.dumps(members))
    aliases,counts = build_history_source_aliases(queries=q,legacy_rows=[row],locators={source.key:('m',)})
    assert counts['mapped']==1
    alias = aliases[source.key]
    assert alias.audience_basis==('legacy_proof' if unknown else 'history_intersection')
    assert alias.audience.members==frozenset(() if members is None else members if unknown else members[:2])
    sources.ledger.persist_aliases(aliases)
    assert sources.ledger.alias(source.key)==alias
    assert sources.verify_source_ref(*source.key)==source
    assert sources.evidence_audience(source,basis='')==alias.audience
    contact(db,'later')
    identifier(db,'later','10004@s.whatsapp.net',start=1)
    event(db,'later-member','member_add',200,{'participants':[['10004@s.whatsapp.net']]})
    assert not sources.permits_principal(source,'whatsapp:10004',now_ms=300)
    if unknown:
        with pytest.raises(Exception) as refused:
            sources.issue('m')
        assert refused.value.code=='denied_unknown_basis'


@pytest.mark.parametrize('null_author',[None,''])
def test_coordinator_null_author_and_projected_contact(case,null_author):
    from yeoman_gateway.knowledge._history_cutover import prepare_legacy_alias_inputs
    _,q,_,db = case
    db.execute("UPDATE messages SET sender_identifier='10001' WHERE message_id='m'")
    source = SourceRef('projected-contact',1,'whatsapp',q.message('m')['chat_id'],'10001',100)
    copies = [proof(q,'m',source),proof(q,'m',source)]
    for item,value in zip(copies,('10001',null_author),strict=True):
        item['original']['state']['author_principal']=value
        item['origin']['row_sha256']=row_sha256(item['original'])
    rows,_,counts = prepare_legacy_alias_inputs(queries=q,legacy_rows=[legacy(source)],preserved_rows=copies)
    assert counts['mapped']==1 and counts['author_unresolved']==0
    assert rows[0]['cutover_status']=='mapped'
    assert copies[1]['original']['state']['author_principal']==null_author


@pytest.mark.parametrize(('change','reason'),[('history','history_sender_contact_unproven'),('legacy','legacy_author_unresolvable')])
def test_alias_contact_refusal_subcases(case,change,reason):
    _,q,_,db=case
    source = SourceRef('contact-refusal',1,'whatsapp',q.message('m')['chat_id'],
                       '99999' if change=='legacy' else '10001',100)
    if change=='history':
        db.execute("UPDATE messages SET sender_contact_id=NULL WHERE message_id='m'")
    row = legacy(source,content_fingerprint=q.content_fingerprint('m'))
    aliases,counts=build_history_source_aliases(queries=q,legacy_rows=[row],locators={source.key:('m',)})
    assert not aliases and counts[reason]==1


def test_author_lookup_after_partial_original_has_message_time(case,monkeypatch):
    from yeoman_gateway.knowledge import _history_cutover as cutover
    _,q,_,_=case
    source=SourceRef('partial-clock',1,'whatsapp',q.message('m')['chat_id'],'10001',100)
    partial,complete=proof(q,'m',source),proof(q,'m',source)
    for item in (partial,complete):
        item['original']['state']['author_principal']='10001'
    for field in ('channel','chat_id','native_message_id','sent_ms','time_certainty'):
        partial['original']['state'].pop(field)
    for item in (partial,complete):
        item['origin']['row_sha256']=row_sha256(item['original'])
    seen=[]
    original=cutover._author_contact
    def observed(queries,value,*,at_ms):
        seen.append(at_ms)
        return original(queries,value,at_ms=at_ms)
    monkeypatch.setattr(cutover,'_author_contact',observed)
    rows,_,counts=cutover.prepare_legacy_alias_inputs(queries=q,legacy_rows=[legacy(source)],
        preserved_rows=[partial,complete])
    assert counts['mapped']==1 and counts['author_unresolved']==0
    assert rows[0]['cutover_status']=='mapped' and seen and set(seen)=={100}


@pytest.mark.parametrize('different_author',[False,True])
def test_legacy_numeric_match_requires_agreeing_issued_author(case,different_author):
    from yeoman_gateway.knowledge._history_sources import _proof
    from yeoman_gateway.knowledge.models import KnowledgeError
    _,q,sources,db=case
    db.execute("UPDATE messages SET sender_basis='numeric_match' WHERE message_id='m'")
    assert _proof(q,'m') is None
    assert _proof(q,'m',legacy_alias=True) is not None
    with pytest.raises(KnowledgeError,match='temporal attribution'):
        sources.issue('m')
    source=SourceRef('numeric-legacy',1,'whatsapp',q.message('m')['chat_id'],
                     'whatsapp:10002' if different_author else '10001',100)
    row=legacy(source,content_fingerprint=q.content_fingerprint('m'))
    aliases,counts=build_history_source_aliases(queries=q,legacy_rows=[row],locators={source.key:('m',)})
    if different_author:
        assert not aliases and counts['author_different_contact']==1
    else:
        assert counts['mapped']==1
        sources.ledger.persist_aliases(aliases)
        assert sources.verify_source_ref(*source.key)==source


@pytest.mark.parametrize('second_state',['done','queued','running','failed','cancelled','skipped'])
def test_handover_completed_job_rerun_only_same_state(capture_case,second_state):
    h,k,_,_=capture_case
    h.add('rerun-source',line=1)
    source=issue(h,k,('rerun-source',))[0]
    with k._store.transaction():
        for job_id,state in (('first','done'),('second',second_state)):
            k._store.execute("INSERT INTO knowledge_jobs(job_id,workspace_id,scope_key,kind,sources_json,extractor_version,state,due_ms,created_ms,updated_ms) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (job_id,'synthetic','synthetic','statement_capture',json.dumps([asdict(source)]),'statement-capture-v1',state,MS,MS,MS))
    with h.snapshot() as snap:
        if second_state!='done':
            with pytest.raises(ValueError,match='^ambiguous_handover_job$'):
                producer(k).prepare_handover(snap,pending=(),processed=(),legacy_boundary=(10,'b'))
            assert k._store.scalar('SELECT count(*) FROM knowledge_history_capture')==0
            return
        receipt=producer(k).prepare_handover(snap,pending=(),processed=(),legacy_boundary=(10,'b'))
        assert receipt['processed']==receipt['pending']==[]
    assert k._store.scalar("SELECT outcome FROM knowledge_history_capture WHERE message_id='rerun-source'")=='published'
    assert [(r['job_id'],r['state']) for r in k._store.query('SELECT job_id,state FROM knowledge_jobs ORDER BY job_id')]==[('first','done'),('second','done')]


@pytest.mark.parametrize('reverse',[False,True])
@pytest.mark.parametrize('completed',[False,True])
def test_duplicate_mapped_keys_prefer_completed_then_earliest(case,reverse,completed):
    from yeoman_gateway.knowledge._history_cutover import prepare_capture_inputs
    from yeoman_shared.raw_archive.records import SourceBoundary
    _,q,sources,db=case
    q.snapshot.sources=(SourceBoundary('whatsapp/synthetic.jsonl',1,100,'synthetic'),)
    db.execute("UPDATE messages SET source_refs=? WHERE message_id='m'", (json.dumps(['whatsapp/synthetic.jsonl#1']),))
    first=SourceRef('wa_journal',1,'whatsapp',q.message('m')['chat_id'],'10001',100)
    second=replace(first,event_id='native-id',author_principal='whatsapp:10001')
    rows=[legacy(first,message_id='m',cutover_status='mapped',created_ms=6,completed=False,boundary=[10,'b'],forward_start=[5,'start'],content_fingerprint=q.content_fingerprint('m')),
          legacy(second,message_id='m',cutover_status='mapped',created_ms=12,completed=completed,boundary=[10,'b'],forward_start=[5,'start'],content_fingerprint=q.content_fingerprint('m'))]
    aliases,counts=build_history_source_aliases(queries=q,legacy_rows=rows,locators={first.key:('m',),second.key:('m',)})
    assert counts['mapped']==2
    sources.ledger.persist_aliases(aliases)
    if reverse:
        rows.reverse()
    summary={}
    inputs=prepare_capture_inputs(queries=q,legacy_boundary=(10,'b'),legacy_rows=rows,jobs=[],summary=summary)
    assert inputs['processed' if completed else 'pending']==(second if completed else first,)
    assert summary['duplicate_mapped_sources']==1
    assert sources.verify_source_ref(*first.key)==first and sources.verify_source_ref(*second.key)==second
    nonpreferred=next(row for row in rows if row['event_id']==('wa_journal' if completed else 'native-id'))
    repeated_summary={}
    repeated=prepare_capture_inputs(queries=q,legacy_boundary=(10,'b'),legacy_rows=[*rows,dict(nonpreferred)],jobs=[],summary=repeated_summary)
    assert repeated==inputs and repeated_summary['duplicate_mapped_sources']==1
    with pytest.raises(ValueError,match='^conflicting_capture_proof$'):
        prepare_capture_inputs(queries=q,legacy_boundary=(10,'b'),
            legacy_rows=[*rows,dict(nonpreferred,completed=not nonpreferred['completed'])],jobs=[])


@pytest.mark.parametrize('conflict',['hint','issued'])
def test_duplicate_mapped_keys_conflicting_contact_refuses(case,conflict):
    from yeoman_gateway.knowledge._history_cutover import prepare_capture_inputs
    _,q,_,_=case
    source=SourceRef('wa_journal',1,'whatsapp',q.message('m')['chat_id'],'10001',100)
    other=replace(source,event_id='native-id',author_principal='whatsapp:10002' if conflict=='issued' else source.author_principal)
    rows=[legacy(source,message_id='m',cutover_status='mapped',completed=True),
          legacy(other,message_id='m',cutover_status='mapped',completed=True,author_contact_id='b' if conflict=='hint' else 'a')]
    with pytest.raises(ValueError,match='^conflicting_capture_proof$'):
        prepare_capture_inputs(queries=q,legacy_boundary=(10,'b'),legacy_rows=rows,jobs=[],summary={})


@pytest.mark.parametrize('in_prefix',[False,True])
def test_historical_backfill_alias_has_no_assignment_prefix_still_requires_classification(capture_case,in_prefix):
    from yeoman_gateway.knowledge._history_cutover import prepare_capture_inputs
    h,k,_,_=capture_case
    h.add('historical',line=1,refs=None if in_prefix else ['backfill/journal.jsonl#1'])
    source=issue(h,k,('historical',))[0]
    row=dict(asdict(source),message_id='historical',cutover_status='mapped',author_contact_id='a',completed=False)
    summary={}
    with h.snapshot() as snap:
        if in_prefix:
            with pytest.raises(ValueError,match='^unclassified_handover_message$'):
                prepare_capture_inputs(queries=HistoryQueries(snap),legacy_boundary=(10,'b'),legacy_rows=[row],jobs=[],summary=summary)
            return
        inputs=prepare_capture_inputs(queries=HistoryQueries(snap),legacy_boundary=(10,'b'),legacy_rows=[row],jobs=[],summary=summary)
        assert inputs==dict(processed=(),pending=(),classifications={})
        assert summary['historical_backfill_aliases']==1
        producer(k).prepare_handover(snap,legacy_boundary=(10,'b'),**inputs)
    assert k._store.scalar('SELECT count(*) FROM knowledge_history_capture')==0
    assert k.history_source_ledger.lookup(*source.key) is not None
