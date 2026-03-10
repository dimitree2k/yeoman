"""Tests for contacts disclosure policy field."""

from yeoman.policy.schema import ChatPolicy, ChatPolicyOverride


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
