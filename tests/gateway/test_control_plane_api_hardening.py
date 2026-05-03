"""Control-plane API security hardening tests."""

from yeoman_gateway.api.server import APIConfig, _auth_token_required


def test_missing_auth_token_requires_auth_on_non_loopback_host() -> None:
    api_config = APIConfig(host="0.0.0.0", auth_token=None)

    assert _auth_token_required(api_config) is True


def test_missing_auth_token_keeps_loopback_dev_mode() -> None:
    api_config = APIConfig(host="127.0.0.1", auth_token=None)

    assert _auth_token_required(api_config) is False
