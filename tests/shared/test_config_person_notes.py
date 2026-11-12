"""Config surface for person notes: camelCase keys, defaults and the route key."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from yeoman_shared.config.loader import (
    camel_to_snake,
    convert_keys,
    convert_to_camel,
    snake_to_camel,
)
from yeoman_shared.config.schema import Config, ProcessingPersonNotesConfig


def test_person_notes_defaults_off() -> None:
    settings = Config().processing.person_notes
    assert isinstance(settings, ProcessingPersonNotesConfig)
    assert settings.mode == "off"
    assert settings.route == "knowledge.notes"
    assert settings.max_people_per_chat == 30
    assert settings.min_new_messages == 5
    assert settings.rebuild_every_seconds == 86_400
    assert settings.daily_prompt_token_cap == 300_000
    assert settings.max_lines_per_person == 6


def test_person_notes_accepts_camel_case_and_forbids_unknown_keys() -> None:
    settings = ProcessingPersonNotesConfig.model_validate(
        convert_keys(
            {
                "mode": "build",
                "route": "custom.notes",
                "maxPeoplePerChat": 7,
                "minNewMessages": 2,
                "rebuildEverySeconds": 3600,
                "dailyPromptTokenCap": 1000,
                "maxLinesPerPerson": 3,
            }
        )
    )
    assert settings.mode == "build"
    assert settings.route == "custom.notes"
    assert settings.max_people_per_chat == 7
    assert settings.min_new_messages == 2
    assert settings.rebuild_every_seconds == 3600
    assert settings.daily_prompt_token_cap == 1000
    assert settings.max_lines_per_person == 3
    with pytest.raises(ValidationError):
        ProcessingPersonNotesConfig.model_validate({"mode": "on"})
    with pytest.raises(ValidationError):
        ProcessingPersonNotesConfig.model_validate({"enabled": True})


def test_person_notes_config_round_trips_through_camel_case() -> None:
    payload = convert_to_camel(Config().model_dump())
    assert payload["processing"]["personNotes"]["mode"] == "off"
    assert payload["processing"]["personNotes"]["dailyPromptTokenCap"] == 300_000
    assert convert_keys(payload)["processing"]["person_notes"]["min_new_messages"] == 5


def test_knowledge_notes_route_defaults_to_a_profile() -> None:
    config = Config()
    assert config.models.routes["knowledge.notes"] == "person_notes"
    assert "person_notes" in config.models.profiles
    profile = config.models.profiles["person_notes"]
    assert profile.kind == "chat"
    assert profile.temperature == 0.0
    assert profile.provider == "deepseek" and profile.structured_output is None  # registry decides
    assert profile.reasoning == {"enabled": False}
    # A dotted lowercase route survives both config-key transformations unchanged.
    route = config.processing.person_notes.route
    assert camel_to_snake(snake_to_camel(route)) == route
    assert snake_to_camel(camel_to_snake(route)) == route
