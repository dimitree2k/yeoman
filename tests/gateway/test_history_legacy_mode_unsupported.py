"""Explicitly disabling retired history behavior is refused at config validation."""

import pytest
from pydantic import ValidationError
from yeoman_shared.config.schema import Config


def test_legacy_mode_false_is_refused_by_direct_config_validation():
    with pytest.raises(ValidationError, match=r"history\.legacyWritersDisabled=false is unsupported"):
        Config(history={"legacyWritersDisabled": False})

    with pytest.raises(ValidationError, match=r"history\.legacyWritersDisabled=false is unsupported"):
        Config.model_validate({"history": {"legacy_writers_disabled": False}})
