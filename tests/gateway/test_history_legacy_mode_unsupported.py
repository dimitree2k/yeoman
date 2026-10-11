"""Dormant legacy mode must fail before gateway startup touches storage."""

from unittest.mock import Mock

import pytest
from yeoman_gateway.app.bootstrap import build_gateway_runtime
from yeoman_shared.config.schema import Config


def test_legacy_mode_is_refused_before_storage_is_constructed(tmp_path, monkeypatch):
    import yeoman_gateway.app.bootstrap as bootstrap

    storage = Mock(side_effect=AssertionError("startup reached storage"))
    monkeypatch.setattr(bootstrap, "build_processing_store", storage)
    config = Config(history={"legacyWritersDisabled": False})

    with pytest.raises(ValueError, match=r"history\.legacyWritersDisabled=false is unsupported"):
        build_gateway_runtime(
            config=config,
            provider=Mock(),
            policy_engine=None,
            policy_path=None,
            workspace=tmp_path,
            bus=Mock(),
        )

    storage.assert_not_called()
