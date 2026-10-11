"""Synthetic selected-reader compositions used by the cutover smoke."""
# ruff: noqa: F811

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tests.gateway.convhist.consumer_fixtures import (  # noqa: F401
    capture_case,
    statement_case,
)


def _probe_record(case, policy_path: Path) -> dict:
    return {
        "receipts": str(policy_path.parent / "probe-receipts"),
        "layout": {
            "knowledge_live": str(case.knowledge._store.db_path),
            "policy_snapshot": str(policy_path),
        },
        "inventory": {
            "reader_smoke": {
                "workspace_id": case.knowledge.workspace_id,
                "channel": "whatsapp",
                "chat_id": case.chat,
                "principal": case.author,
                "phone": case.author.removeprefix("whatsapp:") + "@s.whatsapp.net",
                "message_id": case.message_id,
                "source_event_id": "statement-source",
                "statement_id": case.statement_id,
                "query": "Curated",
                "text": "Synthetic",
                "curated_text": "Curated synthetic statement",
                "at_ms": case.now,
                "owner_scope": True,
                "rights": {
                    "knowledge_read": True,
                    "tools_read": True,
                    "owner_export": True,
                },
            }
        },
    }


async def test_all_six_probes_exercise_selected_adapters(statement_case, tmp_path, monkeypatch, capsys):
    import importlib.util

    from scripts.history_cutover_probes import build_probes

    case = statement_case
    await case.publish(old=True, author_only=False)
    await case.curate()
    case.append("message", "probe-media", ms=case.now + 1, text="Synthetic media",
                media={"type": "image"})
    await case.settle()
    before_extractions = list(case.seen)
    policy_path = tmp_path / "policy.json"
    policy = case.policy.engine.policy.model_dump(mode="json")
    policy["owners"]["whatsapp"] = [case.author.removeprefix("whatsapp:")]
    policy_path.write_text(json.dumps(policy))
    home = tmp_path / "offline-home"
    home.mkdir()
    record = _probe_record(case, policy_path)
    probes = build_probes(record=record, home=home)
    assert tuple(probes) == (
        "knowledge", "whatsapp", "responder", "tools", "participation", "secondary"
    )

    spec = importlib.util.spec_from_file_location(
        "cutover_operator", Path(__file__).parents[2] / "scripts/history_cutover.py"
    )
    operator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(operator)
    selection = {
        "legacyWritersDisabled": True,
        "liveProjectionEnabled": True,
        "readers": dict.fromkeys(operator.READER_ORDER, False),
    }
    generations = set()
    for family, probe in probes.items():
        selection["readers"][family] = True
        for _ in range(2):
            receipt = await asyncio.to_thread(
                operator.offline_reader_smoke,
                family=family,
                db_path=case.path,
                raw_root=case.raw,
                selection=selection,
                probe=probe,
            )
        assert receipt["adapter"] is True and receipt["lease_closed"] is True
        assert receipt["unselected_refused"] is True
        generations.add(receipt["generation"])
        selection["readers"][family] = False
    assert len(generations) == 1
    assert not list(home.iterdir())
    scratch = list((Path(record["receipts"])/"reader-smoke").iterdir())
    assert len(scratch) == 12
    assert all(p.stat().st_mode & 0o777 == 0o700 for p in scratch)
    # The actual CLI installs these same callbacks, without reader owner_ack files.
    import shutil
    import sys

    from scripts import history_cutover_host
    from tests.gateway.test_history_cutover import record as synthetic_record
    (tmp_path/'cli').mkdir()
    record_path, cli_home, cli_record = synthetic_record(tmp_path/'cli')
    shutil.copytree(case.raw,cli_home/'raw',dirs_exist_ok=True)
    from yeoman_gateway.history.convert import bridge_refs
    monkeypatch.setattr(bridge_refs,'node_batch_decoder',lambda _: lambda items: {})
    cli_record['rehearsal_root'] = str(tmp_path)
    cli_record['layout'] = dict(record['layout'],history=str(case.path),raw=str(case.raw))
    cli_record['inventory']['reader_smoke'] = record['inventory']['reader_smoke']
    cli_record['digest'] = operator.record_digest(cli_record)
    record_path.write_text(json.dumps(cli_record))
    baseline_calls = []
    def fake_host(**kwargs):
        def control(action,payload):
            if action == 'capture-frozen-baseline':
                # Baseline behavior is covered separately; this witness exercises real readers.
                acquisition = payload['acquisition']
                assert acquisition['ok'] and acquisition['home'] == str(cli_home)
                assert acquisition['digest'] == operator.record_digest(acquisition)
                assert acquisition['inventory_digest'] == operator.record_digest(cli_record['inventory'])
                baseline_calls.append(action)
                return {'ok':True,'baseline_digest':operator.record_digest(dict(
                    record_digest=cli_record['digest'],acquisition_digest=acquisition['digest']))}
            assert action.startswith('select-')
            return {'ok':True}
        control.mode = 'rehearsal'
        return control
    monkeypatch.setattr(history_cutover_host,'rehearsal_host_controls',fake_host)
    monkeypatch.setattr(operator,'_sequence',lambda _: ['acquire',*[a for f in probes for a in (f'select-{f}',f'smoke-reader-{f}')]])
    monkeypatch.setattr(sys,'argv',['history_cutover.py','cutover','--record',str(record_path),
        '--home',str(cli_home),'--controls','rehearsal','--apply'])
    assert await asyncio.to_thread(operator.main) == 0
    assert json.loads(capsys.readouterr().out)['ok']
    receipt = json.loads((Path(cli_record['receipts'])/'cutover.json').read_text())
    assert baseline_calls == ['capture-frozen-baseline']
    assert receipt['phases'][0]['frozen_baseline_digest']
    assert sum(p['action'].startswith('smoke-reader-') for p in receipt['phases']) == 6
    assert all(p['receipt']['adapter'] for p in receipt['phases'] if p['action'].startswith('smoke-reader-'))
    assert case.seen == before_extractions


@pytest.mark.parametrize("reader_smoke", [None, {}, {"unknown": "value"}])
def test_build_probes_refuses_missing_or_unknown_diagnostics(tmp_path, reader_smoke):
    from scripts.history_cutover_probes import build_probes

    with pytest.raises(ValueError, match="reader_smoke"):
        build_probes(record={"inventory": {"reader_smoke": reader_smoke}}, home=tmp_path)


@pytest.mark.parametrize("principal", ["whatsapp:10001", "10001", "telegram:10001", "whatsapp:"])
def test_build_probes_defers_store_reads_and_requires_canonical_principal(tmp_path, principal):
    from scripts.history_cutover_probes import build_probes

    record = {
        "receipts": str(tmp_path / "deferred-receipts"),
        "layout": {
            "knowledge_live": str(tmp_path / "not-published.db"),
            "policy_snapshot": str(tmp_path / "not-published-policy.json"),
        },
        "inventory": {"reader_smoke": {
            "workspace_id": "synthetic",
            "channel": "whatsapp",
            "chat_id": "synthetic@g.us",
            "principal": principal,
            "phone": "10001",
            "message_id": "synthetic-message",
            "source_event_id": "synthetic-native-id",
            "statement_id": "synthetic-statement",
            "query": "synthetic query",
            "text": "synthetic text",
            "curated_text": "synthetic curated text",
            "at_ms": 1,
            "owner_scope": False,
            "rights": {"knowledge_read": True, "tools_read": True, "owner_export": True},
        }},
    }
    if principal != "whatsapp:10001":
        with pytest.raises(ValueError, match="reader_smoke_inputs_invalid"):
            build_probes(record=record, home=tmp_path / "deferred-home")
        return
    probes = build_probes(record=record, home=tmp_path / "deferred-home")
    assert tuple(probes) == (
        "knowledge", "whatsapp", "responder", "tools", "participation", "secondary"
    )
    with pytest.raises(ValueError, match="reader_smoke_path_missing"):
        probes["knowledge"](None)


@pytest.mark.parametrize('outcome',['pass','not_first','not_disclosed','missing_text','unauthorized_leak'])
async def test_knowledge_probe_multiple_readable_statements(statement_case,tmp_path,monkeypatch,outcome):
    from dataclasses import replace

    from yeoman_gateway.history.context import history_knowledge_scope, history_turn
    from yeoman_gateway.knowledge.models import (
        RecallQuery,
        StatementCandidate,
        TrustedCaptureContext,
    )

    from scripts.history_cutover_probes import build_probes

    case=statement_case
    await case.publish(old=True,author_only=False)
    await case.curate()
    async with history_turn(case.projector) as snapshot:
        with history_knowledge_scope(snapshot,case.knowledge):
            case.knowledge.capture(StatementCandidate('Another readable statement',(case.source,)),
                context=TrustedCaptureContext('synthetic-extra',case.knowledge.policy_revision,'native',(case.source,)))
            ranked=case.knowledge.recall(RecallQuery('Curated'),context=case.read_context(case.author))
            assert len(ranked.statement_ids)>1 and ranked.statement_ids[0]==case.statement_id
            other=case.knowledge.recall(RecallQuery('Another'),context=case.read_context(case.author))
            assert other.statement_ids[0]!=case.statement_id and case.statement_id in other.statement_ids
            assert not case.knowledge.recall(RecallQuery('Curated'),context=case.read_context(case.later)).statement_ids
    policy_path=tmp_path/'policy.json'
    policy=case.policy.engine.policy.model_dump(mode='json')
    policy['owners']={'telegram':['synthetic-owner'],'whatsapp':[]}
    policy_path.write_text(json.dumps(policy))
    record=_probe_record(case,policy_path)
    diagnostics=record['inventory']['reader_smoke']
    diagnostics['owner_scope']=False
    if outcome=='not_first':
        diagnostics['query']='Another'
    elif outcome=='not_disclosed':
        diagnostics['principal']=case.later
    elif outcome=='missing_text':
        diagnostics['curated_text']='Absent curated text'
    observed=[]
    original=type(case.knowledge).recall
    def recall(service,query,*,context,**kwargs):
        result=original(service,query,context=context,**kwargs)
        observed.append((context,result))
        if outcome=='unauthorized_leak' and context.principal_id!=case.author:
            return replace(result,statement_ids=(case.statement_id,),text=diagnostics['curated_text'])
        return result
    monkeypatch.setattr(type(case.knowledge),'recall',recall)
    probes=build_probes(record=record,home=tmp_path/'probe-home')
    def invoke():
        with case.snapshot() as snapshot:
            return probes['knowledge'](snapshot)
    if outcome=='pass':
        result=await asyncio.to_thread(invoke)
        assert result['mapped_source'] and result['curated_disclosure'] and result['unauthorized_denial']
        assert len(observed)==2
        denied_context,denied=observed[-1]
        assert denied_context.principal_id.startswith('whatsapp:')
        assert denied_context.principal_id!=case.author and not denied_context.owner
        assert case.statement_id not in denied.statement_ids
    else:
        error='reader_smoke_unauthorized_disclosure' if outcome=='unauthorized_leak' else 'reader_smoke_curated_disclosure_failed'
        with pytest.raises(ValueError,match='^'+error+'$'):
            await asyncio.to_thread(invoke)


@pytest.mark.parametrize('outcome',['pass','missing_admin','non_owner_leak'])
async def test_secondary_probe_uses_policy_admin_and_refuses_member_export(statement_case,tmp_path,monkeypatch,outcome):
    from yeoman_gateway.history import export

    from scripts.history_cutover_probes import build_probes

    case=statement_case
    await case.publish(old=True,author_only=False)
    await case.curate()
    policy_path=tmp_path/'policy.json'
    policy=case.policy.engine.policy.model_dump(mode='json')
    policy['owners']={'telegram':['synthetic-owner'],'whatsapp':[]} if outcome!='missing_admin' else {}
    policy_path.write_text(json.dumps(policy))
    record=_probe_record(case,policy_path)
    record['inventory']['reader_smoke']['owner_scope']=False
    original=export.read_history_turn
    calls=[]
    def read(snapshot,**kwargs):
        context=kwargs['context']
        calls.append((context,kwargs))
        if outcome=='non_owner_leak' and context.owner and context.principal_id==case.author:
            return {'count':0}
        return original(snapshot,**kwargs)
    monkeypatch.setattr(export,'read_history_turn',read)
    probes=build_probes(record=record,home=tmp_path/'secondary-home')
    def invoke():
        with case.snapshot() as snapshot:
            return probes['secondary'](snapshot)
    if outcome=='pass':
        result=await asyncio.to_thread(invoke)
        assert result['bounded_owner_export'] and result['non_owner_refused'] and result['in_turn_subprocess_refusal']
        assert len(calls)==2
        assert calls[0][0].principal_id=='telegram:synthetic-owner' and calls[0][0].owner
        assert calls[0][0].recipient_principals==frozenset({'telegram:synthetic-owner'})
        assert calls[1][0].principal_id==case.author and calls[1][0].owner
        assert all(kwargs['chat_ids']==(case.chat,) and kwargs['limit']==10 and 'aggregate' not in kwargs for _,kwargs in calls)
    else:
        error='reader_smoke_admin_actor_unavailable' if outcome=='missing_admin' else 'reader_smoke_non_owner_export_allowed'
        with pytest.raises(ValueError,match='^'+error+'$'):
            await asyncio.to_thread(invoke)


async def test_rehearsal_whole_tail_repeats_after_final_phase_failure(statement_case, tmp_path):
    import sqlite3
    import sys
    from contextlib import closing
    from dataclasses import asdict
    from subprocess import CompletedProcess

    from yeoman_gateway.processing.store import ProcessingStore
    from yeoman_shared.raw_archive.records import enumerate_committed

    from scripts import history_cutover as m
    from scripts import history_cutover_host as host
    from scripts.history_cutover_probes import build_probes
    from tests.gateway.test_history_cutover import Controls
    from tests.gateway.test_history_cutover import record as synthetic_record
    from tests.gateway.test_history_cutover_host import raw_status
    from tests.gateway.test_history_cutover_resume import resume_record

    case = statement_case
    await case.publish(old=True, author_only=False)
    await case.curate()
    case.append('message', 'tail-probe-media', ms=case.now+1, text='Synthetic media', media={'type':'image'})
    await case.settle()
    root = tmp_path/'tail'
    root.mkdir()
    path, home, base = synthetic_record(root)
    policy = case.policy.engine.policy.model_dump(mode='json')
    policy['owners']['whatsapp'] = [case.author.removeprefix('whatsapp:')]
    policy_path = root/'policy.json'
    policy_path.write_text(json.dumps(policy))
    knowledge = home/'knowledge.db'
    knowledge.unlink()
    with closing(sqlite3.connect(case.knowledge._store.db_path)) as src, closing(sqlite3.connect(knowledge)) as dst:
        src.backup(dst)
    ProcessingStore(Path(base['inventory']['processing_db'])).close()
    base.update(python=sys.executable, rehearsal_root=str(tmp_path), layout=dict(raw=str(case.raw), history=str(case.path),
        knowledge_live=str(knowledge), policy_snapshot=str(policy_path)))
    base['inventory']['reader_smoke'] = _probe_record(case, policy_path)['inventory']['reader_smoke']
    base['commands'] = {a:[sys.executable,'-c','pass'] for a in ('import-preview','import','owner-preview','owner-append')}
    source = Path(base['inventory']['source_dir'])
    source.mkdir()
    (source/'pin.py').write_text('synthetic pin')
    base['inventory']['pinned_files'] = {str(source/'pin.py'):m._hash(source/'pin.py')}
    (home/'raw-status.json').write_text(json.dumps(raw_status()))
    (home/'bridge-status.json').write_text(json.dumps(dict(outbox=dict(pending=0), queue=dict(inflight=0))))
    base['digest'] = m.record_digest(base)
    path.write_text(json.dumps(base))
    vector = [asdict(s) for s in enumerate_committed(case.raw)]
    tail = m._sequence(base)[m._sequence(base).index('verify-deploy-suppression'):]
    runs = [[], []]
    def controls(run_index):
        rehearsal = host.rehearsal_host_controls(copy_home=home, inventory=base['inventory'],
            rehearsal_root=tmp_path, runner=lambda argv:CompletedProcess(argv, 0,
                json.dumps([str(source/f'{p}/__init__.py') for p in ('gateway','shared','overseer')]), ''))
        prepared = m.preparation_controls(host=rehearsal, probes=build_probes(record=base, home=home))
        fake = Controls(m)
        def execute(action, payload):
            if action in tail:
                runs[run_index].append(action)
            if action == 'functional-smoke':
                resume = payload.get('resume')
                prefix = f'resume-{resume["resume_index"]}-' if resume else ''
                ack = dict(action=action, record_digest=base['digest'], owner_ack=True,
                    proof=dict(inbound_message_id_hash='a'*64, outbound_receipt_hash='b'*64,
                               observed_ms=payload['receipts'][-1]['ended_ns']//1_000_000))
                if resume:
                    ack['resume_digest'] = resume['digest']
                host._write(Path(base['receipts'])/(prefix+'functional-smoke.inputs.json'), b'{}', exclusive=True)
                host._write(Path(base['receipts'])/(prefix+'functional-smoke.owner_ack.json'), json.dumps(ack).encode(), exclusive=True)
            if action.startswith('smoke-reader-'):
                result = prepared(action, payload)
            elif action in tail or action in host.SERVICE_ACTIONS or action in ('capture-frozen-baseline','resume-preflight'):
                result = rehearsal(action, payload)
            else:
                result = fake(action, payload)
                if action == 'freeze-verification-prefix':
                    result['sources'] = vector
            if action == 'start-timers' and run_index == 0:
                raise ValueError('synthetic_final_phase_failure')
            return result
        execute.mode = 'rehearsal'
        return execute
    def first_run():
        with m.injected_controls(controls(0)):
            return m.run_cutover(record=path, home=home, apply=True)
    first = await asyncio.to_thread(first_run)
    assert not first['ok'] and first['failed_phase'] == 'start-timers', Path(first['receipt']).read_text()
    second_path = root/'resume.json'
    resume_record(second_path, base, Path(first['receipt']), candidate='second-code')
    assert m.run_resume(record=second_path, home=home)['start'] == 'verify-deploy-suppression'
    def second_run():
        with m.injected_controls(controls(1)):
            return m.run_resume(record=second_path, home=home, apply=True)
    second = await asyncio.to_thread(second_run)
    assert second['ok'], Path(second['receipt']).read_text()
    # The release pre-check adds another frozen-watermarks call in each run.
    expected = tail.copy()
    expected.insert(expected.index('release-fence'), 'frozen-watermarks')
    assert runs == [expected, expected]
    assert not (home/'reader-operational.db').exists()
    assert not (home/'reader-frozen-sessions').exists()
    assert len(list((Path(base['receipts'])/'reader-smoke').iterdir())) == 12
