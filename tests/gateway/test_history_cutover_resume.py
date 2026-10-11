"""Synthetic resume witnesses; no live stores or host services."""
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from scripts import history_cutover as m
from scripts import history_cutover_inputs as inputs
from tests.gateway.test_history_cutover import Controls, record


def approve(path, value):
    value.update(approved=True, approval='synthetic-owner')
    value['digest'] = m.record_digest(value)
    path.write_text(json.dumps(value))
    token = Path(value['confirmation_token'])
    token.write_text(value['digest'] + '\n')
    token.chmod(0o600)
    return value


def failed(tmp_path, action='health'):
    path, home, base = record(tmp_path)
    source = Path(base['inventory']['source_dir'])
    source.mkdir()
    (source/'pin.py').write_text('synthetic pin\n')
    base['inventory']['pinned_files'] = {str(source/'pin.py'): m._hash(source/'pin.py')}
    base['digest'] = m.record_digest(base)
    path.write_text(json.dumps(base))
    with m.injected_controls(Controls(m, fail=action)):
        result = m.run_cutover(record=path, home=home, apply=True)
    assert not result['ok']
    return path, home, base, Path(result['receipt'])


def resume_record(path, base, parent, *, candidate=None, index=None):
    journal = json.loads(parent.read_bytes())
    k = index or journal.get('resume_index', 0) + 1
    value = dict(version=1, kind='resume', base=base, base_digest=base['digest'],
                 parent_journal=str(parent), parent_journal_sha256=m._hash(parent),
                 resume_index=k, resume_from=journal['failed_phase'], candidate=candidate or journal.get('candidate',base['candidate']),
                 pinned_files=base['inventory']['pinned_files'], window_start_ms=1791532800000,
                 window_end_ms=1791534600000, approved=False, approval=None,
                 confirmation_token=str(Path(base['receipts'])/f'resume-{k}-confirmation.token'))
    return approve(path, value)


@pytest.mark.parametrize(('failure','start'), [('health','validate-capture-handover'),('deploy','deploy')])
def test_resume_completes_from_effective_phase(tmp_path, failure, start):
    _, home, base, parent = failed(tmp_path, failure)
    path = tmp_path/'resume-record.json'
    value = resume_record(path, base, parent)
    plan = m.run_resume(record=path, home=home)
    assert plan['start'] == start and plan['actions'][0] == 'resume-preflight'
    c = Controls(m)
    with m.injected_controls(c):
        result = m.run_resume(record=path, home=home, apply=True)
    assert result['ok'] and not result['fenced']
    journal = json.loads(Path(result['receipt']).read_bytes())
    assert journal['resume_index'] == 1
    assert all(p['record_digest'] == base['digest'] and p['resume_digest'] == value['digest'] for p in journal['phases'])
    assert c.calls[0:2] == ['resume-preflight', start]
    with pytest.raises(ValueError, match='resume_already_recorded_no_retry'):
        m.run_resume(record=path, home=home)


def test_two_resumes_and_combined_payload(tmp_path):
    _, home, base, parent = failed(tmp_path, 'drain-durable-tails')
    path = tmp_path/'resume1.json'
    one = resume_record(path, base, parent)
    c = Controls(m, fail='frozen-watermarks')
    with m.injected_controls(c):
        result = m.run_resume(record=path, home=home, apply=True)
    journal = json.loads(Path(result['receipt']).read_bytes())
    assert journal['failed_phase'] == 'release-fence' and journal['refence']['ok']
    path2 = tmp_path/'resume2.json'
    two = resume_record(path2, base, Path(result['receipt']))
    seen = []
    c = Controls(m)
    def control(action, payload):
        assert payload['record'] == base and payload['resume'] == two
        assert all('receipt' in p and not p.get('failed') for p in payload['receipts'])
        seen.extend(payload['receipts'] if action == 'resume-preflight' else [])
        return c(action, payload)
    control.mode = 'rehearsal'
    with m.injected_controls(control):
        result = m.run_resume(record=path2, home=home, apply=True)
    assert result['ok']
    assert any(p.get('resume_digest') == one['digest'] for p in seen)
    assert any(p['action'] == 'acquire' for p in seen)


@pytest.mark.parametrize('generation', [1, 2])
def test_changed_candidate_reselects_and_enforces_generation(tmp_path, generation):
    _, home, base, parent = failed(tmp_path)
    path = tmp_path/'resume.json'
    resume_record(path, base, parent, candidate='changed-code')
    assert m.run_resume(record=path, home=home)['start'] == 'verify-deploy-suppression'
    c = Controls(m)
    def control(action, payload):
        result = c(action, payload)
        if action.startswith('smoke-reader-'):
            result['generation'] = generation
        return result
    control.mode = 'rehearsal'
    with m.injected_controls(control):
        result = m.run_resume(record=path, home=home, apply=True)
    if generation == 1:
        assert result['ok']
        assert [x for x in c.calls if x.startswith('select-')] == [f'select-{f}' for f in m.READER_ORDER]
    else:
        assert not result['ok']
        assert json.loads(Path(result['receipt']).read_bytes())['error_code'] == 'offline_reader_generation_changed'


@pytest.mark.parametrize(('mutation','code'), [
    ('early','resume_phase_not_resumable'),('ok','resume_parent_not_failed'),
    ('fence_unverified','resume_fence_unverified'),('no_refence','resume_refence_required'),
    ('parent_sha','resume_parent_sha_mismatch'),('phase','resume_phase_receipt_mismatch'),
    ('base','resume_base_digest_mismatch'),('unapproved','approved_record_and_local_controls_required'),
    ('token','resume_confirmation_token_required'),('pause','pause_store_not_indefinite'),
    ('pin','resume_pin_mismatch'),('chain','resume_chain_broken'),
])
def test_resume_refusals(tmp_path, mutation, code):
    _, home, base, parent = failed(tmp_path)
    journal = json.loads(parent.read_bytes())
    if mutation in ('early','ok','fence_unverified','no_refence'):
        if mutation == 'early':
            journal['failed_phase'] = 'import'
        elif mutation == 'ok':
            journal['ok'] = True
        elif mutation == 'fence_unverified':
            journal['fence_unverified'] = True
        else:
            journal.pop('refence')
        parent.write_text(json.dumps(journal))
    path = tmp_path/'resume.json'
    value = resume_record(path, base, parent, index=2 if mutation == 'chain' else None)
    if mutation == 'parent_sha':
        parent.write_text(parent.read_text()+' ')
    elif mutation == 'phase':
        (Path(base['receipts'])/'cutover-01.json').write_text('{}')
    elif mutation == 'base':
        value['base_digest'] = '0'*64
    elif mutation == 'unapproved':
        value['approved'] = False
    elif mutation == 'token':
        Path(value['confirmation_token']).unlink()
    elif mutation == 'pause':
        Path(base['inventory']['pause_path']).write_text('{"global_until_ms":0,"chat_until_ms":{}}')
    elif mutation == 'pin':
        next(iter(map(Path,value['pinned_files']))).write_text('changed')
    if mutation in ('base','unapproved'):
        value['digest'] = m.record_digest(value)
        path.write_text(json.dumps(value))
    with m.injected_controls(Controls(m)), pytest.raises(ValueError, match=code):
        m.run_resume(record=path, home=home, apply=True)
    assert not (Path(base['receipts'])/'resume-1.json').exists()


def test_restore_authenticates_resume_pause_receipts(tmp_path):
    base_path, home, base, parent = failed(tmp_path)
    path = tmp_path/'resume.json'
    resume_record(path, base, parent)
    c = Controls(m)
    def control(action, payload):
        result = c(action, payload)
        if action == 'resume-preflight':
            result['pause_restored_sha256'] = 'b'*64
        return result
    control.mode = 'rehearsal'
    with m.injected_controls(control):
        assert m.run_resume(record=path, home=home, apply=True)['ok']
    assert 'b'*64 in m._attempt_pause_digests(base)
    assert m._load(base_path, home, apply=False) == base
    assert m.restore_prior_set(record=base_path, home=home, failed_snapshot=tmp_path/'failed')['planned']
    with m.injected_controls(c):
        assert m.restore_prior_set(record=base_path, home=home, failed_snapshot=tmp_path/'failed',apply=True)['ok']


def test_resume_record_cli_recomputes_pins_and_numbers(tmp_path, capsys):
    base_path, _, base, parent = failed(tmp_path, 'deploy')
    source = Path(base['inventory']['source_dir'])
    # Git identity is read-only; the synthetic source borrows the checkout's gitdir.
    (source/'.git').write_text((Path.cwd()/'.git').read_text())
    (source/'pin.py').write_text('new pin')
    output = tmp_path/'built.json'
    argv = ['resume-record','--base-record',str(base_path),'--parent-journal',str(parent),
            '--inventory-pins-from',str(source),'--window-start-ms','1791532800000',
            '--window-end-ms','1791534600000','--output',str(output)]
    assert inputs.main(argv) == 0, capsys.readouterr().out
    value = json.loads(output.read_bytes())
    assert value['base'] == base and value['base_digest'] == base['digest']
    assert value['pinned_files'] != base['inventory']['pinned_files']
    assert value['candidate'] == subprocess.check_output(['git','-C',str(source),'rev-parse','HEAD'],text=True).strip()
    assert value['resume_index'] == 1 and value['approved'] is False
    assert value['digest'] == m.record_digest(value)
    journal = dict(json.loads(parent.read_bytes()), failed_phase='import')
    parent.write_text(json.dumps(journal))
    assert inputs.main([*argv[:-1],str(tmp_path/'refused.json')]) == 1
    assert not (tmp_path/'refused.json').exists()


@pytest.mark.parametrize(('failure','count','digest'), [
    (None, 105, 'b7176749a6c76d4fcbed635be56741563934abad69300428f478d437eb7286b9'),
    ('health', 86, '9019592a1295b289896edc2a02c472ac14b84968f1dc6f8d17bac0c750b9e45e'),
])
def test_base_run_receipt_bytes_match_pre_refactor_golden(tmp_path, monkeypatch, failure, count, digest):
    """Hashes captured from b8137fc's _run, with deterministic time and controls."""
    writes = []
    counter = iter(range(1000))
    monkeypatch.setattr(m.time, 'time_ns', lambda: next(counter))
    monkeypatch.setattr(m, '_private_json', lambda path, value: writes.append([path.name, m.canonical_json(value)+'\n']))
    monkeypatch.setattr(m, '_write_refusal', lambda *a, **k: None)
    monkeypatch.setattr(m, 'acquire_cutover_snapshot', lambda **k: dict(ok=True, sources=[]))
    base = dict(mode='rehearsal', digest='a'*64, receipts=str(tmp_path/'receipts'), inventory={}, output=str(tmp_path/'output'))
    with m.injected_controls(Controls(m, fail=failure)):
        m._run(base, tmp_path/'home', m._sequence(base), record_dir=tmp_path)
    assert len(writes) == count
    assert hashlib.sha256(json.dumps(writes, sort_keys=True).encode()).hexdigest() == digest


@pytest.mark.parametrize('mode', ['live', 'rehearsal'])
@pytest.mark.parametrize('origins_valid', [True, False])
def test_host_resume_preflight_and_live_release_baseline(tmp_path, mode, origins_valid):
    from subprocess import CompletedProcess

    from scripts import history_cutover_host as host
    from tests.gateway.test_history_cutover_host import Clock, Runner

    _, home, base, parent = failed(tmp_path)
    path = tmp_path/'resume.json'
    value = resume_record(path, base, parent)
    source = Path(base['inventory']['source_dir'])
    (source/'pyproject.toml').write_text('[project]\nname="synthetic"\n')
    for package in ('gateway','shared','overseer'):
        (source/'packages'/package).mkdir(parents=True)
    runner = Runner()
    runner.runtime = Path(base['inventory']['systemd_runtime_dir'])
    def run(argv):
        if '-c' in argv:
            origins = [str(source/f'package{i}/__init__.py') for i in range(3)] if origins_valid else ['/wrong/__init__.py']*3
            return CompletedProcess(argv, 0, json.dumps(origins), '')
        return runner(argv)
    if mode == 'live':
        controls = host.live_host_controls(inventory=base['inventory'], runner=run, clock=Clock())
    else:
        controls = host.rehearsal_host_controls(copy_home=home, inventory=base['inventory'], rehearsal_root=tmp_path, runner=run)
    phases = [p for p in json.loads(parent.read_bytes())['phases'] if 'receipt' in p]
    payload = dict(record=base, resume=value, home=str(home), operation='cutover', receipts=phases)
    if not origins_valid:
        with pytest.raises(ValueError, match='import_origins_unproven'):
            controls('resume-preflight', payload)
        return
    assert controls('resume-preflight', payload)['imports_verified']
    if mode == 'live':
        for unit in ('yeoman-bridge.service', 'yeoman-gateway.service'):
            runner(['systemctl','--user','start',unit])
        assert controls('release-fence', payload)['ok']
        assert not any(a[:3] == ['systemctl','--user','stop'] for a in runner.calls)


@pytest.mark.parametrize('stale', [False, True])
def test_resume_smoke_ack_binds_latest_release(tmp_path, stale):
    from types import SimpleNamespace

    from scripts import history_cutover_host as host

    _, home, base, parent = failed(tmp_path, 'functional-smoke')
    path = tmp_path/'resume.json'
    resume_record(path, base, parent)
    c = Controls(m)
    def control(action, payload):
        if action == 'functional-smoke':
            releases = [p for p in payload['receipts'] if p['action'] == 'release-fence']
            assert len(releases) == 2
            observed = releases[0 if stale else -1]['ended_ns']//1_000_000
            ack = dict(action=action, record_digest=base['digest'], resume_digest=payload['resume']['digest'], owner_ack=True,
                       proof=dict(inbound_message_id_hash='a'*64, outbound_receipt_hash='b'*64, observed_ms=observed))
            (Path(base['receipts'])/f'resume-{payload["resume"]["resume_index"]}-functional-smoke.owner_ack.json').write_text(json.dumps(ack))
            return host._ack(action, payload, wait=True, clock=SimpleNamespace(monotonic=lambda: 1, time=lambda: releases[-1]['ended_ns']/1e9+1))
        return c(action, payload)
    control.mode = 'rehearsal'
    with m.injected_controls(control):
        result = m.run_resume(record=path, home=home, apply=True)
    assert result['ok'] is not stale
    if stale:
        assert json.loads(Path(result['receipt']).read_bytes())['error_code'] == 'stale_owner_ack'


def test_resume_cli_dry_plan_and_resume_record_second_index(tmp_path, monkeypatch, capsys):
    import sys

    base_path, home, base, parent = failed(tmp_path, 'deploy')
    path = tmp_path/'resume.json'
    resume_record(path, base, parent)
    monkeypatch.setattr(sys, 'argv', ['cutover','resume','--record',str(path),'--home',str(home)])
    assert m.main() == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan['planned'] and plan['resume_index'] == 1 and plan['start'] == 'deploy'
    assert plan['actions'][0] == 'resume-preflight'
    c = Controls(m, fail='deploy')
    with m.injected_controls(c):
        result = m.run_resume(record=path, home=home, apply=True)
    source = Path(base['inventory']['source_dir'])
    (source/'.git').write_text((Path.cwd()/'.git').read_text())
    two = inputs.build_resume_record(base_record=base_path, parent_journal=Path(result['receipt']),
                                    inventory_pins_from=source, window=(1791532800000,1791534600000))
    assert two['resume_index'] == 2
    assert two['confirmation_token'] == str(Path(base['receipts'])/'resume-2-confirmation.token')
    path2 = tmp_path/'resume2.json'
    approve(path2, two)
    assert m.run_resume(record=path2, home=home)['start'] == 'verify-deploy-suppression'


def test_resume_cli_reports_specific_refusal(tmp_path, monkeypatch, capsys):
    import sys

    _, home, base, parent = failed(tmp_path)
    path = tmp_path/'resume.json'
    resume_record(path, base, parent)
    Path(next(iter(base['inventory']['pinned_files']))).write_text('drift')
    monkeypatch.setattr(sys, 'argv', ['cutover','resume','--record',str(path),'--home',str(home)])
    assert m.main() == 1
    assert json.loads(capsys.readouterr().out)['error'] == 'resume_pin_mismatch'


def test_resume_refuses_started_receipt_without_journal(tmp_path):
    _, home, base, parent = failed(tmp_path, 'deploy')
    path = tmp_path/'resume.json'
    resume_record(path, base, parent)
    (Path(base['receipts'])/'resume-1-01-started.json').write_text('{}')
    with pytest.raises(ValueError, match='resume_already_recorded_no_retry'):
        m.run_resume(record=path, home=home)


@pytest.mark.parametrize('changed_candidate', [False, True])
def test_resume_preflight_failure_can_parent_next_resume(tmp_path, monkeypatch, changed_candidate):
    base_path, home, base, parent = failed(tmp_path, 'health')
    source = Path(base['inventory']['source_dir'])
    (source/'.git').write_text((Path.cwd()/'.git').read_text())
    head = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    candidate = head if changed_candidate else base['candidate']
    # Keep the builder's real git call while modelling an unchanged synthetic candidate.
    if not changed_candidate:
        monkeypatch.setattr(inputs.subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(a[0], 0, candidate+'\n', ''))
    path = tmp_path/'resume.json'
    one = resume_record(path, base, parent, candidate=candidate)
    original_plan = m.run_resume(record=path, home=home)
    c = Controls(m)
    def stale_pin(action, payload):
        if action == 'resume-preflight':
            (source/'pin.py').write_text('drift during host preflight')
            return m.resume_preflight(payload['resume'], home, apply=True)
        return c(action, payload)
    stale_pin.mode = 'rehearsal'
    with m.injected_controls(stale_pin):
        result = m.run_resume(record=path, home=home, apply=True)
    journal = json.loads(Path(result['receipt']).read_bytes())
    assert not journal['ok'] and journal['fenced'] and not journal.get('fence_unverified')
    assert journal['failed_phase'] == 'resume-preflight' and journal['error_code'] == 'resume_pin_mismatch'
    assert journal['phases'][0]['failed'] and 'refence' not in journal
    (source/'pin.py').write_text('synthetic pin\n')
    two = inputs.build_resume_record(base_record=base_path, parent_journal=Path(result['receipt']),
                                    inventory_pins_from=source, window=(1791532800000,1791534600000))
    assert two['resume_from'] == one['resume_from']
    second = tmp_path/'resume2.json'
    approve(second, two)
    assert m.run_resume(record=second, home=home)['start'] == original_plan['start']
    with m.injected_controls(Controls(m)):
        assert m.run_resume(record=second, home=home, apply=True)['ok']


def test_resume_preflight_parent_with_completed_tail_refuses(tmp_path):
    base_path, home, base, parent = failed(tmp_path, 'deploy')
    path = tmp_path/'resume.json'
    resume_record(path, base, parent)
    with m.injected_controls(Controls(m, fail='resume-preflight')):
        result = m.run_resume(record=path, home=home, apply=True)
    parent = Path(result['receipt'])
    journal = json.loads(parent.read_bytes())
    phase = dict(action='deploy', record_digest=base['digest'], receipt=dict(ok=True))
    journal['phases'].append(phase)
    parent.write_text(m.canonical_json(journal)+'\n')
    (Path(base['receipts'])/'resume-1-02.json').write_text(m.canonical_json(phase)+'\n')
    source = Path(base['inventory']['source_dir'])
    with pytest.raises(ValueError, match='resume_chain_broken'):
        inputs.build_resume_record(base_record=base_path, parent_journal=parent,
                                   inventory_pins_from=source, window=(1791532800000,1791534600000))
    second = tmp_path/'resume2.json'
    value = resume_record(second, base, parent)
    value['resume_from'] = 'deploy'
    approve(second, value)
    with pytest.raises(ValueError, match='resume_chain_broken'):
        m.run_resume(record=second, home=home)


def test_candidate_change_ignores_live_tail_generations(tmp_path):
    _, home, base, parent = failed(tmp_path, 'verify-effect-deduplication')
    journal = json.loads(parent.read_bytes())
    for number, phase in enumerate(journal['phases'], 1):
        if phase['action'] in ('health', 'drain-durable-tails', 'all-committed-barrier'):
            phase['receipt']['generation'] = 99
            (Path(base['receipts'])/f'cutover-{number:02}.json').write_text(m.canonical_json(phase)+'\n')
    parent.write_text(m.canonical_json(journal)+'\n')
    path = tmp_path/'resume.json'
    resume_record(path, base, parent, candidate='changed-code')
    assert m.run_resume(record=path, home=home)['start'] == 'verify-deploy-suppression'
    with m.injected_controls(Controls(m)):
        result = m.run_resume(record=path, home=home, apply=True)
    assert result['ok'], Path(result['receipt']).read_text()
