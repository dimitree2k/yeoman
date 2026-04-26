"""Tests for IPC and webhook config schema."""

import pytest
from pydantic import ValidationError
from yeoman_shared.config.schema import (
    Config,
    ConsciousnessConfig,
    IpcConfig,
    WebhooksConfig,
    WebhookSourceConfig,
)


def test_ipc_config_defaults() -> None:
    cfg = IpcConfig()
    assert cfg.gateway_socket_path == "~/.yeoman/run/gateway.sock"
    assert cfg.overseer_socket_path == "~/.yeoman/run/overseer.sock"
    assert cfg.command_rate_limit == 10


def test_webhooks_config_disabled_by_default() -> None:
    cfg = WebhooksConfig()
    assert cfg.enabled is False
    assert cfg.sources == {}


def test_webhook_source_config() -> None:
    src = WebhookSourceConfig(
        secret_env="GITHUB_WEBHOOK_SECRET",
        deliver_to={"channel": "whatsapp", "chat_id": "owner"},
    )
    assert src.rate_limit == 30
    assert src.allowed_events is None  # None = allow all


def test_config_has_ipc_and_webhooks() -> None:
    cfg = Config()
    assert isinstance(cfg.ipc, IpcConfig)
    assert isinstance(cfg.webhooks, WebhooksConfig)


def test_consciousness_config_defaults_disabled() -> None:
    cfg = ConsciousnessConfig()
    assert cfg.enabled is False
    assert cfg.owner_dm_default_enabled is False
    assert cfg.cron_hour == 19
    assert cfg.cron_minute == 0
    assert cfg.default_daily_cap == 1
    assert cfg.burst_enabled is False


def test_consciousness_config_accepts_aliases() -> None:
    cfg = ConsciousnessConfig.model_validate(
        {
            "enabled": True,
            "ownerDmDefaultEnabled": True,
            "cronHour": 8,
            "cronMinute": 30,
            "agentMaxIterations": 4,
            "agentMaxInputTokens": 12000,
            "maxSpeakupLengthChars": 240,
            "defaultDailyCap": 2,
            "approvalTimeoutSeconds": 900,
            "burstEnabled": True,
            "burstThresholdMessages": 6,
            "burstWindowMinutes": 10,
        }
    )
    assert cfg.enabled is True
    assert cfg.owner_dm_default_enabled is True
    assert cfg.cron_hour == 8
    assert cfg.burst_enabled is True


def test_consciousness_config_accepts_snake_case_from_loader() -> None:
    cfg = ConsciousnessConfig.model_validate(
        {
            "enabled": True,
            "owner_dm_default_enabled": True,
            "cron_hour": 8,
            "cron_minute": 30,
            "agent_max_iterations": 4,
            "agent_max_input_tokens": 12000,
            "max_speakup_length_chars": 240,
            "default_daily_cap": 2,
            "approval_timeout_seconds": 900,
            "burst_enabled": True,
            "burst_threshold_messages": 6,
            "burst_window_minutes": 10,
        }
    )
    assert cfg.enabled is True
    assert cfg.owner_dm_default_enabled is True
    assert cfg.cron_hour == 8
    assert cfg.default_daily_cap == 2
    assert cfg.burst_enabled is True
    assert cfg.burst_threshold_messages == 6
    assert cfg.burst_window_minutes == 10


@pytest.mark.parametrize(
    "payload",
    [
        {"cronHour": 24},
        {"cronMinute": 60},
        {"defaultDailyCap": 11},
        {"agentMaxIterations": 0},
        {"burstThresholdMessages": 1},
    ],
)
def test_consciousness_config_rejects_invalid_values(payload: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        ConsciousnessConfig.model_validate(payload)


def test_config_has_consciousness() -> None:
    cfg = Config()
    assert isinstance(cfg.consciousness, ConsciousnessConfig)
    assert cfg.consciousness.enabled is False


def test_config_has_consciousness_model_routes() -> None:
    cfg = Config()
    assert cfg.models.routes["consciousness.agent"] == "consciousness_judgment"
    assert cfg.models.routes["consciousness.outcome"] == "consciousness_judgment"
    assert cfg.models.routes["consciousness.taste"] == "consciousness_judgment"
    assert "consciousness_judgment" in cfg.models.profiles
