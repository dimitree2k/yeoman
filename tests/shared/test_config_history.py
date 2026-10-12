"""Retired history selectors are dropped or refused with no compatibility shim."""
import json
import os

import pytest
from pydantic import ValidationError
from yeoman_shared.config.loader import _migrate_config_with_change, load_config
from yeoman_shared.config.schema import Config, UnsupportedHistoryConfigError

FALSE_SELECTORS = [
    ("liveProjectionEnabled", "history.liveProjectionEnabled"),
    ("live_projection_enabled", "history.liveProjectionEnabled"),
    ("legacyWritersDisabled", "history.legacyWritersDisabled"),
    ("legacy_writers_disabled", "history.legacyWritersDisabled"),
    *[(f"readers.{family}", f"history.readers.{family}") for family in
      ("participation", "whatsapp", "responder", "tools", "secondary", "knowledge")],
]


def _payload(path, value):
    if path.startswith("readers."):
        return {"history": {"readers": {path.removeprefix("readers."): value}}}
    return {"history": {path: value}}


@pytest.mark.parametrize("key", [
    "YEOMAN_HISTORY__LIVE_PROJECTION_ENABLED", "YEOMAN_HISTORY__liveProjectionEnabled",
    "yeoman_history__LIVE_PROJECTION_ENABLED", "YEOMAN_HISTORY",
])
@pytest.mark.parametrize("payload", [
    {"tools": {"exec": {"restrictToWorkspace": True}}}, {"configVersion": 2}, None,
])
def test_history_environment_cannot_override_config(tmp_path, monkeypatch, key, payload):
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path))
    monkeypatch.setenv(key, '{"liveProjectionEnabled":false}' if key == "YEOMAN_HISTORY" else "false")
    path = tmp_path / "settings.json"
    if payload is not None:
        path.write_text(json.dumps(payload))
    assert not hasattr(Config(), "history")
    assert not hasattr(Config.model_validate({}), "history")
    loaded = load_config(path)
    assert not hasattr(loaded, "history")
    assert os.environ[key].startswith("{") or os.environ[key] == "false"


@pytest.mark.parametrize("key", [
    "liveProjectionEnabled", "live_projection_enabled", "legacyWritersDisabled",
    "legacy_writers_disabled", "readers",
])
def test_true_retired_values_are_removed_on_migration_and_persisted(tmp_path, monkeypatch, key):
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path))
    if key == "readers":
        history = {"readers": {family: True for family, _ in FALSE_SELECTORS[2:]}}
    else:
        history = {key: True}
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"history": history, "configVersion": 2}))
    config = load_config(path)
    persisted = json.loads(path.read_text())
    assert "history" not in persisted
    assert persisted["configVersion"] == 3
    assert "history" not in config.model_dump()
    assert not hasattr(config, "history")
    assert "history" not in Config.model_fields


@pytest.mark.parametrize(("leaf", "stable_path"), FALSE_SELECTORS)
def test_false_retired_value_is_rejected_by_migration_load_and_direct_config(
    tmp_path, monkeypatch, leaf, stable_path
):
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path))
    payload = _payload(leaf, False)
    message = rf"{stable_path}=false is unsupported"
    with pytest.raises(UnsupportedHistoryConfigError, match=message):
        _migrate_config_with_change(payload)
    with pytest.raises(ValidationError, match=message):
        Config(**payload)
    with pytest.raises(ValidationError, match=message):
        Config.model_validate(payload)

    path = tmp_path / "settings.json"
    original = json.dumps(payload)
    path.write_text(original)
    with pytest.raises(UnsupportedHistoryConfigError, match=message):
        load_config(path)
    assert path.read_text() == original


def test_mixed_camel_and_snake_false_cannot_be_hidden_by_normalization():
    payload = {"history": {"liveProjectionEnabled": True, "live_projection_enabled": False}}
    with pytest.raises(UnsupportedHistoryConfigError, match=r"history\.liveProjectionEnabled=false"):
        _migrate_config_with_change(payload)


def test_history_env_filter_preserves_other_config_sources(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path))
    monkeypatch.setenv("YEOMAN_HISTORY", "invalid json must never decode")
    monkeypatch.setenv("YEOMAN_GATEWAY__PORT", "19001")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key")
    assert Config().gateway.port == 19001
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"gateway": {"port": 19002}}))
    config = load_config(path)
    assert config.gateway.port == 19002
    assert config.providers.openai.api_key == "synthetic-key"
    assert os.environ["YEOMAN_HISTORY"] == "invalid json must never decode"
    assert os.environ["YEOMAN_GATEWAY__PORT"] == "19001"


