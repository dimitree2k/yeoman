"""Tests for IPC and webhook config schema."""

from yeoman_shared.config.schema import Config, IpcConfig, WebhooksConfig, WebhookSourceConfig


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
