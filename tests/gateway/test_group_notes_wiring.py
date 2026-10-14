"""Composition: production bootstrap wires the group-notes read path (Spec B section 9).

Synthetic config and history in a temporary home; no provider is called.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from yeoman_gateway.evaluation.participation_groupnotes_ab import GroupNotesShadowRecorder
from yeoman_gateway.processing.participation_group_notes import GroupNotesReader

from tests.gateway.history_test_helpers import ProjectedHistory

# ── composition ──────────────────────────────────────────────────────────────


def test_production_bootstrap_wires_the_group_notes_read_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from yeoman_gateway.app.bootstrap import build_gateway_runtime
    from yeoman_gateway.bus.queue import MessageBus
    from yeoman_gateway.policy.engine import PolicyEngine
    from yeoman_gateway.policy.schema import PolicyConfig
    from yeoman_shared.config.schema import Config

    from tests.gateway.history_test_helpers import prepare_history_retirement_knowledge

    class NeverProvider:
        def get_default_model(self) -> str:
            return "test/provider"

        async def chat(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("composition must not call an LLM")

    monkeypatch.setattr("yeoman_gateway.processing.model_route.RouteClient",
                        lambda **kwargs: NeverProvider())
    history = ProjectedHistory(tmp_path / "composition-history")
    history.control = lambda *args: None
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path))
    monkeypatch.setattr("yeoman_gateway.app.bootstrap.build_history_projector",
                        lambda *args: history)
    config = Config.model_validate({
        "knowledge": {"enabled": True, "capture_enabled": False,
                      "db_path": str(prepare_history_retirement_knowledge(tmp_path, "kb"))},
        "security": {"enabled": False},
        "personaEvolution": {"enabled": False},
        "processing": {"enabled": True, "chats": ["whatsapp:group@g.us"],
                       "participation": {"enabled": True, "shadow": False,
                                         "judgeRoute": "participation.judge",
                                         "groupNotes": "shadow"}},
    })
    policy = PolicyEngine(PolicyConfig.model_validate({"channels": {"whatsapp": {
        "chats": {"group@g.us": {"participation": {"enabled": True}}}
    }}}), workspace=tmp_path)
    runtime = build_gateway_runtime(config=config, provider=NeverProvider(), policy_engine=policy,
                                    policy_path=None, workspace=tmp_path / "workspace",
                                    bus=MessageBus())
    try:
        decision = runtime.participation_decision
        assert decision is not None
        reader = decision._context_builder._group_notes  # noqa: SLF001
        assert isinstance(reader, GroupNotesReader)
        assert reader._knowledge is not None  # noqa: SLF001
        assert reader.mode() == "shadow"
        recorder = decision._group_notes_shadow  # noqa: SLF001
        assert isinstance(recorder, GroupNotesShadowRecorder)
        assert recorder.directory == tmp_path / "var" / "eval" / "participation-groupnotes-ab"
    finally:
        history.close()
        runtime.speakup_log.close()
        runtime.processing.close()
