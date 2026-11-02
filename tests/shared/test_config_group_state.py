"""Config defaults for the shadow group-state service."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from yeoman_shared.config.loader import (
    camel_to_snake,
    convert_keys,
    convert_to_camel,
    snake_to_camel,
)
from yeoman_shared.config.schema import Config, GroupStateConfig, KnowledgeConfig


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


def test_labels_config_defaults_off() -> None:
    labels = Config().processing.group_state.labels
    assert labels.mode == "off"
    assert labels.chats == []
    assert labels.route == "conversation.labels"
    assert labels.max_per_chat_day == 40
    assert labels.max_global_day == 80
    assert labels.max_tokens_day == 400_000
    assert labels.min_messages == 4
    assert labels.refresh_after_messages == 30
    assert labels.max_input_chars == 6_000
    assert labels.max_conversations == 3
    assert labels.timeout_seconds == 30


def test_labels_config_accepts_camel_case() -> None:
    labels = GroupStateConfig.model_validate(
        convert_keys(
            {
                "labels": {
                    "mode": "on",
                    "chats": ["whatsapp:group@g.us"],
                    "route": "custom.labels",
                    "maxPerChatDay": 10,
                    "maxGlobalDay": 20,
                    "maxTokensDay": 1_000,
                    "minMessages": 2,
                    "refreshAfterMessages": 5,
                    "maxInputChars": 500,
                    "maxConversations": 2,
                    "timeoutSeconds": 5,
                }
            }
        )
    ).labels
    assert labels.mode == "on"
    assert labels.chats == ["whatsapp:group@g.us"]
    assert labels.route == "custom.labels"
    assert labels.max_per_chat_day == 10
    assert labels.max_global_day == 20
    assert labels.max_tokens_day == 1_000
    assert labels.min_messages == 2
    assert labels.refresh_after_messages == 5
    assert labels.max_input_chars == 500
    assert labels.max_conversations == 2
    assert labels.timeout_seconds == 5


def test_labels_config_forbids_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        GroupStateConfig.model_validate({"labels": {"enabled": True}})
    with pytest.raises(ValidationError):
        GroupStateConfig.model_validate({"labels": {"mode": "maybe"}})


def test_labels_default_route_survives_config_key_round_trip() -> None:
    # models.routes keys are written camelCase and read snake_case; the default must survive.
    route = GroupStateConfig().labels.route
    assert camel_to_snake(snake_to_camel(route)) == route
    assert snake_to_camel(camel_to_snake(route)) == route


def test_knowledge_group_reply_recall_defaults_off_and_round_trips() -> None:
    assert Config().knowledge.group_reply_recall == "off"

    configured = KnowledgeConfig.model_validate(convert_keys({"groupReplyRecall": "on"}))
    assert configured.group_reply_recall == "on"

    payload = convert_to_camel(Config().model_dump())
    assert payload["knowledge"]["groupReplyRecall"] == "off"
    assert convert_keys(payload)["knowledge"]["group_reply_recall"] == "off"
