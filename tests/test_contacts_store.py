"""Tests for ContactsStore SQLite operations."""

import pytest
from pathlib import Path

from yeoman.contacts.store import ContactsStore


@pytest.fixture
def store(tmp_path: Path) -> ContactsStore:
    return ContactsStore(db_path=tmp_path / "contacts.db")


class TestContactsStore:
    def test_create_contact(self, store: ContactsStore) -> None:
        contact = store.create_contact(display_name="Alex", phone_number="+491521234567")
        assert contact.display_name == "Alex"
        assert contact.phone_number == "+491521234567"
        assert contact.id

    def test_get_contact(self, store: ContactsStore) -> None:
        created = store.create_contact(display_name="Alex")
        fetched = store.get_contact(created.id)
        assert fetched is not None
        assert fetched.display_name == "Alex"

    def test_get_contact_not_found(self, store: ContactsStore) -> None:
        assert store.get_contact("nonexistent") is None

    def test_update_display_name(self, store: ContactsStore) -> None:
        c = store.create_contact(display_name="Unknown")
        store.update_display_name(c.id, "Alex")
        updated = store.get_contact(c.id)
        assert updated is not None
        assert updated.display_name == "Alex"

    def test_add_identifier(self, store: ContactsStore) -> None:
        c = store.create_contact(display_name="Alex")
        ident = store.add_identifier(contact_id=c.id, channel="whatsapp", identifier="491521234567@s.whatsapp.net", kind="phone_jid")
        assert ident.contact_id == c.id

    def test_lookup_by_identifier(self, store: ContactsStore) -> None:
        c = store.create_contact(display_name="Alex")
        store.add_identifier(contact_id=c.id, channel="whatsapp", identifier="491521234567@s.whatsapp.net", kind="phone_jid")
        found = store.lookup_by_identifier("whatsapp", "491521234567@s.whatsapp.net")
        assert found is not None
        assert found.id == c.id

    def test_lookup_by_identifier_not_found(self, store: ContactsStore) -> None:
        assert store.lookup_by_identifier("whatsapp", "nonexistent") is None

    def test_upsert_alias_insert(self, store: ContactsStore) -> None:
        c = store.create_contact(display_name="Alex")
        store.upsert_alias(contact_id=c.id, alias="AlexGrrrrr", source="pushname")
        aliases = store.get_aliases(c.id)
        assert len(aliases) == 1
        assert aliases[0].alias == "AlexGrrrrr"

    def test_upsert_alias_update_last_seen(self, store: ContactsStore) -> None:
        c = store.create_contact(display_name="Alex")
        store.upsert_alias(contact_id=c.id, alias="AlexGrrrrr", source="pushname")
        store.upsert_alias(contact_id=c.id, alias="AlexGrrrrr", source="pushname")
        aliases = store.get_aliases(c.id)
        assert len(aliases) == 1

    def test_add_field(self, store: ContactsStore) -> None:
        c = store.create_contact(display_name="Alex")
        f = store.add_field(contact_id=c.id, kind="email", value="alex@bmw.de", label="work")
        assert f.kind == "email"
        assert f.label == "work"

    def test_get_fields(self, store: ContactsStore) -> None:
        c = store.create_contact(display_name="Alex")
        store.add_field(contact_id=c.id, kind="email", value="a@b.com")
        store.add_field(contact_id=c.id, kind="url", value="linkedin.com/in/alex", label="linkedin")
        fields = store.get_fields(c.id)
        assert len(fields) == 2

    def test_load_all_identifiers(self, store: ContactsStore) -> None:
        c1 = store.create_contact(display_name="Alex")
        c2 = store.create_contact(display_name="Bob")
        store.add_identifier(contact_id=c1.id, channel="whatsapp", identifier="jid1", kind="phone_jid")
        store.add_identifier(contact_id=c2.id, channel="whatsapp", identifier="jid2", kind="phone_jid")
        mapping = store.load_all_identifiers()
        assert mapping["jid1"] == c1.id
        assert mapping["jid2"] == c2.id

    def test_search_by_display_name(self, store: ContactsStore) -> None:
        store.create_contact(display_name="Alex")
        store.create_contact(display_name="Bob")
        results = store.search_by_display_name("alex")
        assert len(results) == 1
        assert results[0].display_name == "Alex"

    def test_search_by_alias(self, store: ContactsStore) -> None:
        c = store.create_contact(display_name="Alex")
        store.upsert_alias(contact_id=c.id, alias="Pikachu123", source="pushname")
        results = store.search_by_alias("pikachu123")
        assert len(results) == 1
        assert results[0].id == c.id

    def test_merge_contacts(self, store: ContactsStore) -> None:
        target = store.create_contact(display_name="Alex")
        store.add_identifier(contact_id=target.id, channel="whatsapp", identifier="jid1", kind="phone_jid")
        stub = store.create_contact(display_name="Unknown")
        store.add_identifier(contact_id=stub.id, channel="whatsapp", identifier="jid2", kind="phone_jid")
        store.upsert_alias(contact_id=stub.id, alias="NewName", source="pushname")
        store.merge_contacts(target_id=target.id, source_id=stub.id)
        assert store.get_contact(stub.id) is None
        found = store.lookup_by_identifier("whatsapp", "jid2")
        assert found is not None
        assert found.id == target.id
        aliases = store.get_aliases(target.id)
        assert any(a.alias == "NewName" for a in aliases)

    def test_delete_field(self, store: ContactsStore) -> None:
        c = store.create_contact(display_name="Alex")
        f = store.add_field(contact_id=c.id, kind="email", value="old@test.com")
        store.delete_field(f.contact_id, f.kind, f.value)
        assert len(store.get_fields(c.id)) == 0

    def test_get_identifiers(self, store: ContactsStore) -> None:
        c = store.create_contact(display_name="Alex")
        store.add_identifier(contact_id=c.id, channel="whatsapp", identifier="jid1", kind="phone_jid")
        store.add_identifier(contact_id=c.id, channel="whatsapp", identifier="lid1", kind="lid")
        idents = store.get_identifiers(c.id)
        assert len(idents) == 2
