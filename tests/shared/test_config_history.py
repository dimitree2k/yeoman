"""History activation is controlled by file/initializer data only."""
import json
import os

import pytest
from yeoman_shared.config.loader import load_config
from yeoman_shared.config.schema import Config

ENV_KEYS = ['YEOMAN_HISTORY__LIVE_PROJECTION_ENABLED', 'YEOMAN_HISTORY__liveProjectionEnabled', 'yeoman_history__LIVE_PROJECTION_ENABLED', 'YEOMAN_HISTORY']


@pytest.mark.parametrize('key', ENV_KEYS)
@pytest.mark.parametrize('payload', [{'tools': {'exec': {'restrictToWorkspace': True}}}, {'configVersion': 2}, {'configVersion': 2, 'history': {'liveProjectionEnabled': False}}, None])
def test_history_environment_cannot_activate_loader_or_bootstrap(tmp_path, monkeypatch, key, payload):
    monkeypatch.setenv('YEOMAN_HOME', str(tmp_path))
    monkeypatch.setenv(key, '{"liveProjectionEnabled":true}' if key == 'YEOMAN_HISTORY' else 'true')
    path = tmp_path / 'settings.json'
    if payload is not None:
        path.write_text(json.dumps(payload))
    for config in (Config(), Config.model_validate({}), load_config(path), load_config(path)):
        assert config.history.live_projection_enabled is False
    assert os.environ[key].endswith('true}') or os.environ[key] == 'true'


def test_history_file_true_is_honored_without_environment_override(tmp_path, monkeypatch):
    monkeypatch.setenv('YEOMAN_HOME', str(tmp_path))
    monkeypatch.setenv('YEOMAN_HISTORY__LIVE_PROJECTION_ENABLED', 'false')
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({'history': {'liveProjectionEnabled': True}}))
    assert load_config(path).history.live_projection_enabled
    assert load_config(path).history.live_projection_enabled
    assert Config(history={'live_projection_enabled': True}).history.live_projection_enabled
    assert Config.model_validate({'history': {'liveProjectionEnabled': True}}).history.live_projection_enabled


def test_history_env_filter_preserves_other_config_sources(tmp_path, monkeypatch):
    monkeypatch.setenv('YEOMAN_HOME', str(tmp_path))
    monkeypatch.setenv('YEOMAN_HISTORY', 'invalid json must never decode')
    monkeypatch.setenv('YEOMAN_GATEWAY__PORT', '19001')
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-key')
    assert Config().gateway.port == 19001
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({'gateway': {'port': 19002}}))
    config = load_config(path)
    assert config.gateway.port == 19002
    assert config.providers.openai.api_key == 'synthetic-key'
    assert os.environ['YEOMAN_HISTORY'] == 'invalid json must never decode'
    assert os.environ['YEOMAN_GATEWAY__PORT'] == '19001'


@pytest.mark.parametrize("key", [
    "YEOMAN_HISTORY", "yeOMaN_HisTory__READERS", "YEOMAN_HISTORY__READERS__TOOLS",
    "yeoman_history__readers__whatsapp", "YEOMAN_HISTORY__legacyWritersDisabled",
    "yeOMAN_history__LEGACY_WRITERS_DISABLED", "YEOMAN_HISTORY__LIVE_PROJECTION_ENABLED",
    *[f"{prefix}__READERS__{family}" for prefix in ("YEOMAN_HISTORY", "yeOMaN_HisTory")
      for family in ("PARTICIPATION", "WHATSAPP", "RESPONDER", "TOOLS", "SECONDARY", "KNOWLEDGE")],
])
@pytest.mark.parametrize("dotenv", [False, True])
def test_history_selectors_off_and_environment_cannot_activate(tmp_path, monkeypatch, key, dotenv):
    from yeoman_gateway.app.bootstrap import build_history_projector

    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path))
    value = "invalid-json-before-decode" if key.casefold() in ("yeoman_history", "yeoman_history__readers") else "true"
    envfile = tmp_path / "synthetic.env"
    if dotenv:
        envfile.write_text(f"{key}={value}\n")
    else:
        monkeypatch.setenv(key, value)
    for data in ({}, {"history": {"liveProjectionEnabled": False}}):
        config = Config(_env_file=envfile if dotenv else None, **data)
        assert not config.history.live_projection_enabled
        assert not config.history.legacy_writers_disabled
        assert not any(config.history.readers.model_dump().values())
        assert build_history_projector(config, object()) is None
    for enabled in (False, True):
        data = {"history": {"liveProjectionEnabled": enabled, "readers": {"tools": True, "whatsapp": True},
                            "legacyWritersDisabled": False}}
        path = tmp_path / "synthetic-settings.json"
        path.write_text(json.dumps(data))
        for config in (Config(_env_file=envfile if dotenv else None, **data), load_config(path)):
            selected = {family for family, value in config.history.readers.model_dump().items()
                        if config.history.live_projection_enabled and value}
            assert selected == ({"tools", "whatsapp"} if enabled else set())
            assert not config.history.legacy_writers_disabled
            if not enabled:
                assert build_history_projector(config, object()) is None
    assert Config(history={"legacyWritersDisabled": True}).history.legacy_writers_disabled
    assert not (tmp_path / "data" / "operational" / "history").exists()


def test_direct_writer_flag_is_file_only_and_does_not_normalize_config(tmp_path, monkeypatch):
    from yeoman_gateway.history.writer_guard import (
        LegacyHistoryWriterDisabled,
        legacy_history_writers_disabled,
        require_legacy_history_writer,
    )

    root = tmp_path / 'not-created'
    monkeypatch.setenv('YEOMAN_HOME', str(root))
    monkeypatch.setenv('YEOMAN_HISTORY__legacyWritersDisabled', 'true')
    require_legacy_history_writer(disabled=legacy_history_writers_disabled(), channel='whatsapp')
    assert not root.exists()
    root.mkdir()
    path = root / 'config.json'
    text = '{"history":{"legacyWritersDisabled":true}}'
    path.write_text(text)
    with pytest.raises(LegacyHistoryWriterDisabled):
        require_legacy_history_writer(disabled=legacy_history_writers_disabled(), channel='whatsapp')
    require_legacy_history_writer(disabled=True, channel='telegram')
    assert path.read_text() == text
    assert sorted(p.name for p in root.iterdir()) == ['config.json']
