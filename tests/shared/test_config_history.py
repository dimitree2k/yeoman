"""Retired history selectors are discarded when old files are normalized."""
import json

import pytest
from yeoman_shared.config.loader import load_config


@pytest.mark.parametrize("history", [
    {"liveProjectionEnabled": False, "legacyWritersDisabled": False,
     "readers": {name: False for name in ("participation", "whatsapp", "responder", "tools", "secondary", "knowledge")}},
    {"live_projection_enabled": True, "legacy_writers_disabled": True,
     "readers": {"tools": True}},
])
def test_old_history_config_loads_and_normalizes(tmp_path, history):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"configVersion": 2, "history": history, "gateway": {"port": 19002}, "memory": {"dbPath": "synthetic-retired.db", "mode": "shadow", "shared": {"enabled": True}, "wal": {"stateDir": "retired-session-state"}}, "knowledge": {"legacyMemoryPaths": ["synthetic.db"], "legacyContactsPaths": ["synthetic.db"]}}))
    config = load_config(path)
    assert config.gateway.port == 19002
    assert config.config_version == 3
    assert "history" not in config.model_dump()
    normalized = json.loads(path.read_text())
    assert normalized["configVersion"] == 3
    assert "history" not in normalized
    assert not {"dbPath", "mode", "shared", "wal"}.intersection(normalized["memory"])
    assert not {"legacyMemoryPaths", "legacyContactsPaths"}.intersection(normalized["knowledge"])
    assert load_config(path).model_dump() == config.model_dump()
