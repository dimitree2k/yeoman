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
