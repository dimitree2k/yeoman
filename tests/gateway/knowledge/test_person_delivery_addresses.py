"""T04/T18: delivery addresses come from proven bindings behind an allowed address.

The delivery side of the public facade answers exactly one question: *which proven
platform address may this name be delivered to?*  Recognising a name is not permission to
address somebody with it (spec 7.3), a withdrawn name is not an address, and two
candidates stay two candidates - a caller that wants one address refuses, it does not
take the first row.  All values here are synthetic.
"""

from __future__ import annotations

import pytest
from yeoman_gateway.knowledge.models import Identifier

SCOPE_A = "channel:whatsapp:chat:group-a"
SCOPE_B = "channel:whatsapp:chat:group-b"


def _alias_id(h, person_id: str, name: str) -> int:
    row = h.service._store.query_one(  # noqa: SLF001 - locating the row under test
        "SELECT id FROM contact_aliases WHERE contact_id = ? AND alias = ?",
        (person_id, name),
    )
    assert row is not None, f"no alias {name!r} for {person_id}"
    return int(row["id"])


def _binding_id(h, person_id: str) -> str:
    row = h.service._store.query_one(  # noqa: SLF001 - locating the binding under test
        "SELECT binding_id FROM knowledge_identifier_bindings"
        " WHERE person_id = ? AND status = 'active' ORDER BY binding_id LIMIT 1",
        (person_id,),
    )
    assert row is not None, f"no active binding for {person_id}"
    return str(row["binding_id"])


def _allow_address(h, person_id: str, name: str, *, scope: str = SCOPE_A) -> None:
    h.service.observe_alias(
        person_id=person_id,
        name=name,
        alias_kind="nickname",
        scope_key=scope,
        evidence_ref=f"evidence-{name}",
        status="confirmed",
        address_allowed=True,
    )


def test_an_address_allowed_alias_returns_the_proven_delivery_address(knowledge_harness):
    h = knowledge_harness
    person = h.person("Vinzent")
    _allow_address(h, person, "Vinz")

    delivered = h.service.delivery_identifiers_for_alias(
        "Vinz", channel="whatsapp", scope_key=SCOPE_A
    )

    assert [item.value for item in delivered] == ["4910000000002@s.whatsapp.net"]
    assert delivered[0].kind == "phone_jid"
    # Case-folded, but never a partial match.
    assert h.service.delivery_identifiers_for_alias("vinz", channel="whatsapp") != ()
    assert h.service.delivery_identifiers_for_alias("Vin", channel="whatsapp") == ()


def test_recognition_alone_is_not_an_address(knowledge_harness):
    """The push name is observed (searchable) but not released for addressing."""
    h = knowledge_harness
    person = h.person("Tom")

    assert h.service.person_id_for_value("4910000000002@s.whatsapp.net") == person
    assert h.service.delivery_identifiers_for_alias("Tom", channel="whatsapp") == ()


def test_two_people_with_one_alias_stay_ambiguous(knowledge_harness):
    h = knowledge_harness
    first = h.person("Tom")
    second = h.person_for("whatsapp:4910000000099", "Tom")
    _allow_address(h, first, "Tomi")
    _allow_address(h, second, "Tomi")

    delivered = h.service.delivery_identifiers_for_alias("Tomi", channel="whatsapp")

    assert {item.value for item in delivered} == {
        "4910000000002@s.whatsapp.net",
        "4910000000099@s.whatsapp.net",
    }


def test_a_withdrawn_or_retracted_alias_is_not_an_address(knowledge_harness):
    h = knowledge_harness
    person = h.person("Wimsekt")
    _allow_address(h, person, "Wim")
    assert h.service.delivery_identifiers_for_alias("Wim", channel="whatsapp") != ()

    h.service.retire_alias(
        alias_id=_alias_id(h, person, "Wim"),
        context=h.admin_context(),
        expected_revision=h.identity_revision(),
        reason="not_wanted",
    )

    assert h.service.delivery_identifiers_for_alias("Wim", channel="whatsapp") == ()


def test_an_ended_binding_leaves_no_delivery_address(knowledge_harness):
    h = knowledge_harness
    person = h.person("Maria")
    _allow_address(h, person, "Mari")

    h.service.end_binding(
        binding_id=_binding_id(h, person),
        context=h.admin_context(),
        expected_revision=h.identity_revision(),
    )

    assert h.service.delivery_identifiers_for_alias("Mari", channel="whatsapp") == ()


def test_a_context_alias_is_not_a_global_address(knowledge_harness):
    h = knowledge_harness
    person = h.person("Vinzent")
    _allow_address(h, person, "Vinz", scope=SCOPE_A)

    assert h.service.delivery_identifiers_for_alias(
        "Vinz", channel="whatsapp", scope_key=SCOPE_A
    )
    assert (
        h.service.delivery_identifiers_for_alias("Vinz", channel="whatsapp", scope_key=SCOPE_B)
        == ()
    )
    # Without a stated context nothing is narrowed away - the caller decides.
    assert h.service.delivery_identifiers_for_alias("Vinz", channel="whatsapp")


def test_identifier_for_name_refuses_two_kinds_and_honours_a_kind_preference(
    knowledge_harness,
):
    """A person with a phone JID and a LID has no *implicit* single address."""
    h = knowledge_harness
    resolved = h.observe(
        "whatsapp",
        "phone_jid",
        "4910000000002@s.whatsapp.net",
        mapping=True,
        name="Tom",
        extra=(Identifier("whatsapp", "lid", "46918273106072@lid"),),
    )
    person = resolved.person_id
    assert person

    assert h.service.identifier_for_name("Tom", channel="whatsapp") is None
    preferred = h.service.identifier_for_name(
        "Tom", channel="whatsapp", prefer_kind="phone_jid"
    )
    assert preferred is not None
    assert preferred.value == "4910000000002@s.whatsapp.net"
    # Two people with the name stay two people, preference or not.
    h.person_for("whatsapp:4910000000099", "Tom")
    assert (
        h.service.identifier_for_name("Tom", channel="whatsapp", prefer_kind="phone_jid")
        is None
    )


@pytest.mark.parametrize("alias", ["", "   ", "Vin"])
def test_empty_or_partial_lookups_never_resolve(knowledge_harness, alias):
    h = knowledge_harness
    person = h.person("Vinzent")
    _allow_address(h, person, "Vinz")
    assert h.service.delivery_identifiers_for_alias(alias, channel="whatsapp") == ()
