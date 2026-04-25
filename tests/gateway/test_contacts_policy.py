"""Tests for chat policy fields."""

import pytest
from pydantic import ValidationError
from yeoman_gateway.policy.engine import PolicyEngine
from yeoman_gateway.policy.schema import ChatPolicy, ChatPolicyOverride, PolicyConfig


class TestContactsDisclosurePolicy:
    def test_default_is_false(self) -> None:
        policy = ChatPolicy()
        assert policy.contacts_disclosure is False

    def test_can_enable(self) -> None:
        policy = ChatPolicy.model_validate({"contactsDisclosure": True})
        assert policy.contacts_disclosure is True

    def test_override_default_is_none(self) -> None:
        override = ChatPolicyOverride()
        assert override.contacts_disclosure is None

    def test_override_can_set(self) -> None:
        override = ChatPolicyOverride.model_validate({"contactsDisclosure": True})
        assert override.contacts_disclosure is True


class TestSpontaneityPolicy:
    def test_default_is_disabled(self) -> None:
        policy = ChatPolicy()
        assert policy.spontaneity.enabled is False
        assert policy.spontaneity.profile == "off"
        assert policy.spontaneity.daily_cap is None

    def test_accepts_valid_spontaneity_fields(self) -> None:
        policy = ChatPolicy.model_validate(
            {
                "spontaneity": {
                    "enabled": True,
                    "profile": "careful",
                    "dailyCap": 1,
                    "allowedActions": ["surface_memory", "observation"],
                    "preview": "owner_dm",
                    "quietHoursStart": "22:00",
                    "quietHoursEnd": "07:00",
                }
            }
        )

        assert policy.spontaneity.enabled is True
        assert policy.spontaneity.profile == "careful"
        assert policy.spontaneity.daily_cap == 1
        assert policy.spontaneity.allowed_actions == ["surface_memory", "observation"]
        assert policy.spontaneity.preview == "owner_dm"
        assert policy.spontaneity.quiet_hours_start == "22:00"
        assert policy.spontaneity.quiet_hours_end == "07:00"

    @pytest.mark.parametrize(
        "payload",
        [
            {"spontaneity": {"dailyCap": 11}},
            {"spontaneity": {"preview": "group_direct"}},
            {"spontaneity": {"allowedActions": ["delete_everything"]}},
            {"spontaneity": {"unknown": True}},
        ],
    )
    def test_rejects_invalid_spontaneity_fields(self, payload: dict[str, object]) -> None:
        with pytest.raises(ValidationError):
            ChatPolicy.model_validate(payload)

    def test_override_default_is_none(self) -> None:
        override = ChatPolicyOverride()
        assert override.spontaneity is None

    def test_policy_engine_exposes_resolved_spontaneity(self, tmp_path) -> None:
        config = PolicyConfig.model_validate(
            {
                "defaults": {
                    "spontaneity": {
                        "enabled": False,
                        "profile": "off",
                        "dailyCap": 0,
                    }
                },
                "channels": {
                    "whatsapp": {
                        "default": {
                            "spontaneity": {
                                "profile": "careful",
                                "dailyCap": 1,
                                "preview": "owner_dm",
                            }
                        },
                        "chats": {
                            "group@g.us": {
                                "spontaneity": {
                                    "enabled": True,
                                    "allowedActions": ["observation"],
                                }
                            }
                        },
                    }
                },
            }
        )

        resolved = PolicyEngine(config, workspace=tmp_path).resolve_policy("whatsapp", "group@g.us")

        assert resolved.spontaneity_enabled is True
        assert resolved.spontaneity_profile == "careful"
        assert resolved.spontaneity_daily_cap == 1
        assert resolved.spontaneity_allowed_actions == ["observation"]
        assert resolved.spontaneity_preview == "owner_dm"
