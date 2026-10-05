from hist_fixtures import make_db
from yeoman_gateway.history.convert.identity_stores import (
    convert_chat_registry,
    convert_contacts_db,
    convert_knowledge,
)

KNOWLEDGE = """
CREATE TABLE contacts (id TEXT, display_name TEXT, phone_number TEXT, is_owner INTEGER, created_at TEXT,
  status TEXT, preferred_name TEXT);
CREATE TABLE contact_identifiers (channel TEXT, identifier TEXT, contact_id TEXT, kind TEXT);
CREATE TABLE contact_aliases (id INTEGER, contact_id TEXT, alias TEXT, source TEXT, status TEXT,
  mapping_retracted INTEGER);
CREATE TABLE knowledge_identifier_bindings (binding_id TEXT, channel TEXT, kind TEXT, namespace TEXT,
  value TEXT, person_id TEXT, status TEXT, valid_from_ms INTEGER, valid_until_ms INTEGER,
  mapping_verified INTEGER);
CREATE TABLE knowledge_provider_pair_evidence (channel TEXT, namespace TEXT, phone_value TEXT,
  lid_value TEXT, first_observed_at_ms INTEGER, last_observed_at_ms INTEGER);
CREATE TABLE knowledge_provider_pair_sources (channel TEXT, phone_value TEXT, lid_value TEXT);
"""


def test_knowledge(tmp_path):
    make_db(tmp_path / "data/knowledge/knowledge.db", KNOWLEDGE, {
        "contacts": [{"id": "945ae43e", "display_name": "Frank Taeger", "phone_number": "+4917632625469",
                      "is_owner": 0, "created_at": "2026-03-10T10:00:00+00:00", "status": "active",
                      "preferred_name": None}],
        "contact_identifiers": [{"channel": "whatsapp", "identifier": "4917632625469@s.whatsapp.net",
                                 "contact_id": "945ae43e", "kind": "phone_jid"}],
        "contact_aliases": [{"id": 1, "contact_id": "945ae43e", "alias": "Frank", "source": "observed",
                             "status": "active", "mapping_retracted": 0}],
        "knowledge_identifier_bindings": [{"binding_id": "b1", "channel": "whatsapp", "kind": "lid",
                                           "namespace": "default", "value": "46918273106072@lid",
                                           "person_id": "31025a3e", "status": "active",
                                           "valid_from_ms": 0, "valid_until_ms": 0, "mapping_verified": 1}],
        "knowledge_provider_pair_evidence": [{"channel": "whatsapp", "namespace": "default",
                                              "phone_value": "31612477403@s.whatsapp.net",
                                              "lid_value": "278408672067690@lid",
                                              "first_observed_at_ms": 1, "last_observed_at_ms": 2}],
        "knowledge_provider_pair_sources": [{"channel": "whatsapp", "phone_value": "x", "lid_value": "y"}],
    })
    lines = list(convert_knowledge(tmp_path))
    by_kind = {}
    for line in lines:
        by_kind.setdefault(line["kind"], []).append(line)
    contact = by_kind["contact_record"][0]
    assert contact["channel"] == "any" and contact["payload"]["contactRef"] == "945ae43e"
    assert contact["payload"]["displayName"] == "Frank Taeger" and contact["payload"]["isOwner"] is False
    assert isinstance(contact["payload"]["createdMs"], int)
    ident, binding = by_kind["identifier_record"]
    assert ident["payload"] == {"contactRef": "945ae43e", "identifier": "4917632625469@s.whatsapp.net",
                                "identifierKind": "phone_jid", "source": "contact_identifiers"}
    assert binding["payload"]["contactRef"] == "31025a3e" and binding["payload"]["source"] == "binding"
    assert binding["payload"]["status"] == "active" and "validFromMs" not in binding["payload"]
    assert by_kind["name_record"][0]["payload"]["name"] == "Frank"
    assert by_kind["pair_record"][0]["payload"] == {"lid": "278408672067690@lid",
                                                    "pnJid": "31612477403@s.whatsapp.net",
                                                    "firstMs": 1, "lastMs": 2}
    assert by_kind["identity_detail"][0]["skip_reason"] == "not_projected:knowledge_provider_pair_sources"
    assert all(x["provenance"] == "derived_only" for x in lines)


def test_contacts_db_and_chat_registry(tmp_path):
    make_db(tmp_path / "data/contacts/contacts.db", """
        CREATE TABLE contacts (id TEXT, display_name TEXT, phone_number TEXT, is_owner INTEGER, created_at TEXT);
        CREATE TABLE contact_fields (id INTEGER, contact_id TEXT, kind TEXT, value TEXT);""", {
        "contacts": [{"id": "e31b9a09", "display_name": "Dimi", "phone_number": None, "is_owner": 1,
                      "created_at": "2026-03-10T10:00:00"}],
        "contact_fields": [{"id": 1, "contact_id": "e31b9a09", "kind": "city", "value": "Düsseldorf"}],
    })
    make_db(tmp_path / "data/inbound/chat_registry.db",
            "CREATE TABLE chats (channel TEXT, chat_id TEXT, readable_name TEXT);",
            {"chats": [{"channel": "whatsapp", "chat_id": "1-2@g.us", "readable_name": "Boys"}]})
    contact, field = list(convert_contacts_db(tmp_path))
    assert contact["payload"]["isOwner"] is True and contact["origin"]["store"] == "contacts_db"
    assert field["skip_reason"] == "not_projected:contact_fields"
    (chat,) = list(convert_chat_registry(tmp_path))
    assert (chat["kind"], chat["skip_reason"]) == ("chat_record", "chat_metadata")
