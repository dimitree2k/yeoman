"""Config defaults for the shadow group-state service."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from yeoman_shared.config.loader import convert_keys
from yeoman_shared.config.schema import Config, GroupStateConfig


def test_config_default_off() -> None:
    cfg = Config()
    assert isinstance(cfg.processing.group_state, GroupStateConfig)
    assert cfg.processing.group_state.mode == "off"
    assert cfg.processing.group_state.chats == []
    assert cfg.processing.group_state.min_interval_seconds == 30
    assert cfg.processing.group_state.rebuild_every_seconds == 600
    assert cfg.processing.group_state.window_hours == 24
    assert cfg.processing.group_state.people_window_days == 7
    assert cfg.processing.group_state.actions_limit == 20
    assert cfg.processing.group_state.snapshots_kept == 20
    assert cfg.processing.group_state.db_path == "data/ops/group-state.db"


def test_camel_case_keys_are_accepted() -> None:
    cfg = GroupStateConfig.model_validate(
        convert_keys(
            {
                "mode": "shadow",
                "chats": ["whatsapp:group@g.us"],
                "minIntervalSeconds": 45,
                "rebuildEverySeconds": 900,
                "windowHours": 12,
                "peopleWindowDays": 3,
                "actionsLimit": 10,
                "snapshotsKept": 5,
                "dbPath": "data/ops/custom-state.db",
            }
        )
    )
    assert cfg.mode == "shadow"
    assert cfg.chats == ["whatsapp:group@g.us"]
    assert cfg.min_interval_seconds == 45
    assert cfg.rebuild_every_seconds == 900
    assert cfg.window_hours == 12
    assert cfg.people_window_days == 3
    assert cfg.actions_limit == 10
    assert cfg.snapshots_kept == 5
    assert cfg.db_path == "data/ops/custom-state.db"


def test_unknown_keys_and_bad_mode_are_rejected() -> None:
    with pytest.raises(ValidationError):
        GroupStateConfig.model_validate({"mode": "enforce"})
    with pytest.raises(ValidationError):
        GroupStateConfig.model_validate({"enabled": True})
