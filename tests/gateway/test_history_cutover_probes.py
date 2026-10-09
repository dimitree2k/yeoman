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
    # The actual CLI installs these same callbacks, without reader owner_ack files.
    import shutil
    import sys

    from scripts import history_cutover_host
    from tests.gateway.test_history_cutover import record as synthetic_record
    (tmp_path/'cli').mkdir()
    record_path, cli_home, cli_record = synthetic_record(tmp_path/'cli')
    shutil.copytree(case.raw,cli_home/'raw',dirs_exist_ok=True)
    cli_record['layout'] = dict(record['layout'],history=str(case.path),raw=str(case.raw))
    cli_record['inventory']['reader_smoke'] = record['inventory']['reader_smoke']
    cli_record['digest'] = operator.record_digest(cli_record)
    record_path.write_text(json.dumps(cli_record))
    def fake_host(**kwargs):
        def control(action,payload):
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
    assert sum(p['action'].startswith('smoke-reader-') for p in receipt['phases']) == 6
    assert all(p['receipt']['adapter'] for p in receipt['phases'] if p['action'].startswith('smoke-reader-'))
    assert case.seen == before_extractions


@pytest.mark.parametrize("reader_smoke", [None, {}, {"unknown": "value"}])
def test_build_probes_refuses_missing_or_unknown_diagnostics(tmp_path, reader_smoke):
    from scripts.history_cutover_probes import build_probes

    with pytest.raises(ValueError, match="reader_smoke"):
        build_probes(record={"inventory": {"reader_smoke": reader_smoke}}, home=tmp_path)


def test_build_probes_defers_store_reads_until_callback(tmp_path):
    from scripts.history_cutover_probes import build_probes

    record = {
        "layout": {
            "knowledge_live": str(tmp_path / "not-published.db"),
            "policy_snapshot": str(tmp_path / "not-published-policy.json"),
        },
        "inventory": {"reader_smoke": {
            "workspace_id": "synthetic",
            "channel": "whatsapp",
            "chat_id": "synthetic@g.us",
            "principal": "whatsapp:10001",
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
    probes = build_probes(record=record, home=tmp_path / "deferred-home")
    assert tuple(probes) == (
        "knowledge", "whatsapp", "responder", "tools", "participation", "secondary"
    )
    with pytest.raises(ValueError, match="reader_smoke_path_missing"):
        probes["knowledge"](None)
