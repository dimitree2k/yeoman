from __future__ import annotations

import pytest
from pydantic import ValidationError
from yeoman_shared.config.loader import convert_keys
from yeoman_shared.config.schema import ProcessingConfig


def test_short_reply_is_off_by_default() -> None:
    config = ProcessingConfig()
    assert config.short_reply.mode == "off"
    assert config.short_reply.mode_for("whatsapp", "g@g.us") == "off"
    assert config.short_reply.fallback_emojis == ["👍", "🤙", "😄"]
    assert config.short_reply.max_output_tokens == 48


def test_short_reply_reads_the_runtime_json_shape() -> None:
    raw = {
        "shortReply": {
            "mode": "shadow",
            "route": "reaction.decide",
            "maxChars": 60,
            "rateLimit": {"windowSeconds": 90},
            "chats": ["whatsapp:g@g.us"],
        }
    }
    config = ProcessingConfig.model_validate(convert_keys(raw))
    assert config.short_reply.mode == "shadow"
    assert config.short_reply.max_chars == 60
    assert config.short_reply.rate_limit.window_seconds == 90
    assert config.short_reply.rate_limit.count == 2


def test_mode_applies_only_to_listed_chats() -> None:
    config = ProcessingConfig.model_validate(
        {"short_reply": {"mode": "live", "chats": ["whatsapp:a@g.us"]}}
    )
    assert config.short_reply.mode_for("whatsapp", "a@g.us") == "live"
    assert config.short_reply.mode_for("whatsapp", "b@g.us") == "off"


def test_mode_is_off_until_builder_injects_the_effective_managed_chats() -> None:
    config = ProcessingConfig.model_validate({"short_reply": {"mode": "shadow"}})
    assert config.short_reply.mode_for("whatsapp", "any@g.us") == "off"
    scoped = config.short_reply.model_copy(update={"chats": ["whatsapp:managed@g.us"]})
    assert scoped.mode_for("whatsapp", "managed@g.us") == "shadow"
    assert scoped.mode_for("whatsapp", "unmanaged@g.us") == "off"


def test_fallback_emojis_must_be_in_the_owner_vocabulary() -> None:
    with pytest.raises(ValidationError, match="fallbackEmojis"):
        ProcessingConfig.model_validate(
            {
                "reaction_emojis": ["👍"],
                "short_reply": {"mode": "shadow", "fallback_emojis": ["🤙"]},
            }
        )


def test_an_empty_vocabulary_stays_valid_while_short_replies_are_off() -> None:
    config = ProcessingConfig.model_validate({"reaction_emojis": []})
    assert config.reaction_emojis == [] and config.short_reply.mode == "off"
    silent = ProcessingConfig.model_validate(
        {"reaction_emojis": [], "short_reply": {"mode": "live", "fallback": "silence"}}
    )
    assert silent.short_reply.fallback == "silence"


def test_unknown_mode_and_unknown_keys_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ProcessingConfig.model_validate({"short_reply": {"mode": "loud"}})
    with pytest.raises(ValidationError):
        ProcessingConfig.model_validate({"short_reply": {"surprise": True}})
