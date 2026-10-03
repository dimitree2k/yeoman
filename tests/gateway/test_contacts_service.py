"""Tests for ContactsService."""

from pathlib import Path

import pytest
from yeoman_gateway.knowledge._contacts.service import ContactsService


@pytest.fixture
def service(tmp_path: Path) -> ContactsService:
    return ContactsService(db_path=tmp_path / "contacts.db")


class TestContactsService:
    def test_boot_loads_known_jids(self, service: ContactsService) -> None:
        c = service.store.create_contact(display_name="Alex")
        service.store.add_identifier(
            contact_id=c.id, channel="whatsapp",
            identifier="491521234567@s.whatsapp.net", kind="phone_jid",
        )
        service.reload_cache()
        assert "491521234567@s.whatsapp.net" in service.known_jids

    def test_ensure_contact_creates_stub(self, service: ContactsService) -> None:
        contact_id = service.ensure_contact(
            channel="whatsapp",
            identifier="491521234567@s.whatsapp.net",
            kind="phone_jid",
            push_name="Alex",
        )
        assert contact_id is not None
        contact = service.store.get_contact(contact_id)
        assert contact is not None
        assert contact.display_name == "Alex"
        assert "491521234567@s.whatsapp.net" in service.known_jids

    def test_ensure_contact_returns_existing(self, service: ContactsService) -> None:
        first = service.ensure_contact(
            channel="whatsapp", identifier="jid1", kind="phone_jid", push_name="Alex",
        )
        second = service.ensure_contact(
            channel="whatsapp", identifier="jid1", kind="phone_jid", push_name="Alex",
        )
        assert first == second

    def test_ensure_contact_tracks_alias(self, service: ContactsService) -> None:
        service.ensure_contact(
            channel="whatsapp", identifier="jid1", kind="phone_jid", push_name="Alex",
        )
        service.ensure_contact(
            channel="whatsapp", identifier="jid1", kind="phone_jid", push_name="AlexGrrrrr",
        )
        cid = service.known_jids["jid1"]
        aliases = service.store.get_aliases(cid)
        assert len(aliases) == 2

    def test_get_display_name(self, service: ContactsService) -> None:
        cid = service.ensure_contact(
            channel="whatsapp", identifier="jid1", kind="phone_jid", push_name="Alex",
        )
        assert service.get_display_name(cid) == "Alex"

    def test_get_display_name_unknown(self, service: ContactsService) -> None:
        assert service.get_display_name("nonexistent") is None

    def test_update_display_name_invalidates_cache(self, service: ContactsService) -> None:
        cid = service.ensure_contact(
            channel="whatsapp", identifier="jid1", kind="phone_jid", push_name="Alex",
        )
        assert service.get_display_name(cid) == "Alex"
        service.update_display_name(cid, "Alexander")
        assert service.get_display_name(cid) == "Alexander"

    def test_resolve_name_to_jid(self, service: ContactsService) -> None:
        service.ensure_contact(
            channel="whatsapp", identifier="jid1@s.whatsapp.net",
            kind="phone_jid", push_name="Alex",
        )
        jid = service.resolve_name_to_jid("Alex", channel="whatsapp")
        assert jid == "jid1@s.whatsapp.net"

    def test_resolve_name_to_jid_case_insensitive(self, service: ContactsService) -> None:
        service.ensure_contact(
            channel="whatsapp", identifier="jid1@s.whatsapp.net",
            kind="phone_jid", push_name="Alex",
        )
        jid = service.resolve_name_to_jid("alex", channel="whatsapp")
        assert jid == "jid1@s.whatsapp.net"

    def test_resolve_name_ambiguous_returns_none(self, service: ContactsService) -> None:
        service.ensure_contact(
            channel="whatsapp", identifier="jid1", kind="phone_jid", push_name="Alex",
        )
        service.ensure_contact(
            channel="whatsapp", identifier="jid2", kind="phone_jid", push_name="Alex",
        )
        jid = service.resolve_name_to_jid("Alex", channel="whatsapp")
        assert jid is None

    def test_resolve_name_ambiguous_with_group_hint(self, service: ContactsService) -> None:
        service.ensure_contact(
            channel="whatsapp", identifier="jid1", kind="phone_jid", push_name="Alex",
        )
        service.ensure_contact(
            channel="whatsapp", identifier="jid2", kind="phone_jid", push_name="Alex",
        )
        jid = service.resolve_name_to_jid(
            "Alex", channel="whatsapp", group_participants=["jid1"],
        )
        assert jid == "jid1"

    def test_mark_owner(self, service: ContactsService) -> None:
        cid = service.ensure_contact(
            channel="whatsapp", identifier="owner_jid",
            kind="phone_jid", push_name="Dimi",
        )
        service.mark_owner_from_policy({"whatsapp": ["owner_jid"]})
        contact = service.store.get_contact(cid)
        assert contact is not None
        assert contact.is_owner is True

    def test_mark_owner_matches_bare_policy_digits_to_a_phone_jid(
        self, service: ContactsService
    ) -> None:
        """Policy lists WhatsApp owners as bare digits; bindings store the phone JID."""
        cid = service.ensure_contact(
            channel="whatsapp", identifier="491520000009@s.whatsapp.net",
            kind="phone_jid", push_name="Owner",
        )
        service.mark_owner_from_policy({"whatsapp": ["491520000009"]})
        contact = service.store.get_contact(cid)
        assert contact is not None
        assert contact.is_owner is True

    def test_mark_owner_matches_an_e164_policy_number_to_a_phone_jid(
        self, service: ContactsService
    ) -> None:
        """The live policy writes WhatsApp owners as ``+49...``, like the policy matcher."""
        cid = service.ensure_contact(
            channel="whatsapp", identifier="491520000009@s.whatsapp.net",
            kind="phone_jid", push_name="Owner",
        )
        service.mark_owner_from_policy({"whatsapp": ["+491520000009"]})
        contact = service.store.get_contact(cid)
        assert contact is not None
        assert contact.is_owner is True

    def test_mark_owner_never_turns_bare_digits_into_a_lid(
        self, service: ContactsService
    ) -> None:
        cid = service.ensure_contact(
            channel="whatsapp", identifier="491520000009@lid", kind="lid", push_name="Other",
        )
        service.mark_owner_from_policy({"whatsapp": ["491520000009"]})
        contact = service.store.get_contact(cid)
        assert contact is not None
        assert contact.is_owner is False

    def test_mark_owner_clears_a_flag_policy_no_longer_supports(
        self, service: ContactsService
    ) -> None:
        owner = service.ensure_contact(
            channel="whatsapp", identifier="491520000009@s.whatsapp.net",
            kind="phone_jid", push_name="Owner",
        )
        stale = service.ensure_contact(
            channel="whatsapp", identifier="100000000000007@lid", kind="lid", push_name="Stub",
        )
        service.store.set_owner(stale, is_owner=True)

        service.mark_owner_from_policy({"whatsapp": ["+491520000009"]})

        assert service.store.get_contact(owner).is_owner is True
        assert service.store.get_contact(stale).is_owner is False

    def test_mark_owner_keeps_flags_when_policy_names_no_owner(
        self, service: ContactsService
    ) -> None:
        """An empty owner map is a missing policy, not a decision to demote everyone."""
        flagged = service.ensure_contact(
            channel="whatsapp", identifier="491520000009@s.whatsapp.net",
            kind="phone_jid", push_name="Owner",
        )
        service.store.set_owner(flagged, is_owner=True)

        service.mark_owner_from_policy({})

        assert service.store.get_contact(flagged).is_owner is True


def test_upsert_field_delegates_to_store(tmp_path: Path) -> None:
    from yeoman_gateway.knowledge._contacts.service import ContactsService
    svc = ContactsService(db_path=tmp_path / "c.db")
    c = svc.store.create_contact(display_name="Frank")
    svc.upsert_field(contact_id=c.id, kind="person_profile", value="Frank: is a doctor")
    fields = svc.store.get_fields(c.id)
    assert len(fields) == 1
    assert fields[0].kind == "person_profile"
