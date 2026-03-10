"""Tests for ContactsService."""

import pytest
from pathlib import Path

from yeoman.contacts.service import ContactsService


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
