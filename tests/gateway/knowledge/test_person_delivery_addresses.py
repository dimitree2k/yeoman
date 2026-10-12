"""Native contact names are not delivery authorization."""

from __future__ import annotations


def test_native_contact_name_does_not_grant_alias_address(knowledge_harness):
    h = knowledge_harness
    person = h.person("Tom")
    phone = h.principal_for(person).split(":", 1)[1] + "@s.whatsapp.net"

    assert h.service.person_id_for_value(phone) == person
    assert h.service.delivery_identifiers_for_alias("Tom", channel="whatsapp") == ()
    assert h.service.identifier_for_name("Tom", channel="whatsapp") is None


def test_partial_native_contact_name_never_resolves_to_an_address(knowledge_harness):
    h = knowledge_harness
    h.person("Vinzent")

    assert h.service.identifier_for_name("Vin", channel="whatsapp") is None
    assert h.service.delivery_identifiers_for_alias("Vin", channel="whatsapp") == ()
