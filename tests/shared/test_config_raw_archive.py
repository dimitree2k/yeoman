from __future__ import annotations

import pytest
from pydantic import ValidationError
from yeoman_shared.config.loader import convert_keys
from yeoman_shared.config.schema import Config


def test_raw_archive_is_on_by_default_with_owner_media_limits() -> None:
    raw = Config().raw
    assert raw.enabled is True
    assert raw.media.enabled is True
    assert raw.media.max_video_bytes == 50 * 1024 * 1024


def test_raw_archive_reads_camel_case_json() -> None:
    cfg = Config.model_validate(convert_keys({"raw": {"media": {"maxVideoBytes": 1000}}}))
    assert cfg.raw.media.max_video_bytes == 1000


def test_negative_video_limit_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Config.model_validate(convert_keys({"raw": {"media": {"maxVideoBytes": -1}}}))
