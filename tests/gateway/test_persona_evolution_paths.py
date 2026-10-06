from __future__ import annotations

from pathlib import Path

from yeoman_gateway.cli.persona_evolution_commands import _default_output_path, _state_db_path


def test_persona_evolution_cli_ledger_stays_in_workspace(tmp_path):
    workspace = Path(tmp_path) / "workspace"
    assert _state_db_path(workspace) == workspace / "persona-evolution" / "persona-evolution.db"


def test_persona_evolution_cli_proposal_stays_in_data_archive(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path))
    proposal = _default_output_path("personas/default.md")
    assert proposal.parent == tmp_path / "data" / "persona-evolution" / "proposals"
