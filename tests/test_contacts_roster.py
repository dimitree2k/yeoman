"""Tests for contact roster assembly and injection."""

import pytest
from pathlib import Path

from yeoman.contacts.service import ContactsService


@pytest.fixture
def contacts(tmp_path: Path) -> ContactsService:
    return ContactsService(db_path=tmp_path / "contacts.db")


class TestBuildContactRoster:
    def test_builds_roster_for_known_participants(
        self, contacts: ContactsService,
    ) -> None:
        cid = contacts.ensure_contact(
            channel="whatsapp", identifier="jid1",
            kind="phone_jid", push_name="Alex",
        )
        contacts.store.add_field(contact_id=cid, kind="note", value="drives a black M3")
        contacts.store.add_field(contact_id=cid, kind="email", value="alex@bmw.de", label="work")

        roster = contacts.build_roster(
            channel="whatsapp",
            participant_jids=["jid1", "jid_unknown"],
        )
        assert len(roster) == 1
        assert roster[0]["name"] == "Alex"
        assert "drives a black M3" in roster[0]["facts"]
        assert "email (work): alex@bmw.de" in roster[0]["facts"]

    def test_excludes_owner_from_roster(self, contacts: ContactsService) -> None:
        cid = contacts.ensure_contact(
            channel="whatsapp", identifier="owner_jid",
            kind="phone_jid", push_name="Dimi",
        )
        contacts.store.set_owner(cid, is_owner=True)
        contacts.store.add_field(contact_id=cid, kind="note", value="secret info")

        roster = contacts.build_roster(
            channel="whatsapp",
            participant_jids=["owner_jid"],
        )
        assert len(roster) == 0  # Owner excluded

    def test_format_roster_text(self, contacts: ContactsService) -> None:
        cid = contacts.ensure_contact(
            channel="whatsapp", identifier="jid1",
            kind="phone_jid", push_name="Alex",
        )
        contacts.store.add_field(contact_id=cid, kind="note", value="drives a black M3")

        text = contacts.format_roster_text(
            channel="whatsapp",
            participant_jids=["jid1"],
        )
        assert "[Group Members]" in text
        assert "Alex" in text
        assert "drives a black M3" in text
