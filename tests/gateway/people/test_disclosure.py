import json
from dataclasses import asdict, replace

import pytest
from yeoman_gateway.history.live import HistoryPaused
from yeoman_gateway.people.disclosure import reader_context, render_person
from yeoman_gateway.people.resolver import PeopleUnavailable, resolve_person

PRIVATE_KEYS = ("address", "home_city", "birthday", "relation", "notes")


@pytest.fixture
def detailed_person(directory_case):
    c = directory_case
    p = c.add_person(
        "Vincent",
        ids=(
            ("phone", "4915000000011"),
            ("wa_lid", "300000000001"),
            ("telegram_id", "1234567"),
            ("knowledge_person", "legacy-private"),
        ),
    )
    for key in PRIVATE_KEYS:
        # Preserve the schema's birthday validation with an unmistakable synthetic date.
        c.writer.execute(
            f"UPDATE persons SET {key}=? WHERE id=?",
            ("--02-29" if key == "birthday" else "PRIVATE_" + key, p),
        )
    c.writer.execute("UPDATE persons SET preferred_language='de' WHERE id=?", (p,))
    c.add_name(p, "Visible Alias", source="owner")
    c.add_name(p, "Hidden Alias", hidden=True)
    c.add_id(p, "phone", "4915000000012", end=200)
    c.add_id(p, "phone", "4915000000013", start=400)
    c.add_id(p, "phone", "4915000000014", hidden=True)
    c.set_roster("g@g.us", [p, c.owner, c.counterpart, c.assistant], label="Shared Group")
    c.set_roster("private@g.us", [p, c.assistant], label="Private Group")
    return p


@pytest.mark.parametrize(
    "kind,phone,details",
    [("owner_dm", True, True), ("group", True, False), ("other_dm", False, False)],
)
def test_disclosure_context_matrix(directory_case, detailed_person, kind, phone, details):
    c = directory_case
    card = render_person(detailed_person, c.reader(kind))
    assert card.display_name == "Vincent"
    assert card.names == ("Vincent", "Visible Alias")
    assert card.phones == (("4915000000011",) if phone else ())
    assert bool(card.owner_details) is details
    encoded = json.dumps(asdict(card))
    for key in PRIVATE_KEYS:
        sentinel = "--02-29" if key == "birthday" else "PRIVATE_" + key
        assert (sentinel in encoded) is details
    assert "preferred_language" not in encoded and "Hidden Alias" not in encoded
    assert "legacy-private" not in encoded
    for suffix in ("12", "13", "14"):
        assert "49150000000" + suffix not in encoded
    assert card.shared_chat_names == ("Shared Group",)
    assert (
        ("phone", "4915000000011") in card.identifiers
        if phone
        else not any(k == "phone" for k, v in card.identifiers)
    )


def test_group_former_author_has_names_but_no_phone(directory_case):
    c = directory_case
    p = c.add_person("Former", ids=(("phone", "4915000000011"),))
    c.add_author("g@g.us", p, at_ms=100)
    c.set_roster("g@g.us", [])
    reader = c.reader("group")
    assert resolve_person("Former", reader).person_id == p
    card = render_person(p, reader)
    assert card.names == ("Former",) and card.phones == () and card.owner_details == ()
    assert card.identifiers == ()


@pytest.mark.parametrize("where", ["group", "wrong_dm"])
def test_owner_flag_outside_owner_dm_never_grants_details(directory_case, detailed_person, where):
    c = directory_case
    chat = "g@g.us" if where == "group" else c.endpoint(c.counterpart)
    reader = reader_context(
        c.directory,
        channel="whatsapp",
        chat_id=chat,
        principal_id="whatsapp:" + c.endpoint(c.owner),
        is_owner=True,
        at_ms=300,
        history=c.snapshot,
    )
    assert reader.kind == ("group" if where == "group" else "other_dm")
    card = render_person(detailed_person, reader)
    assert card.owner_details == ()
    if where == "wrong_dm":
        assert card.phones == ()


def test_ambiguous_labels_only_show_shared_chats(directory_case):
    c = directory_case
    a, b = c.add_person("Sam Twins"), c.add_person("Sam Twins")
    c.set_roster("g@g.us", [a, b, c.counterpart, c.assistant], label="Shared Group")
    c.set_roster("private@g.us", [a, c.owner, c.assistant], label="Private Group")
    reader = c.reader("other_dm")
    result = resolve_person("Sam Twins", reader)
    assert result.status == "ambiguous"
    assert [r.person_id for r in result.candidates] == [a, b]
    assert [render_person(r.person_id, reader).shared_chat_names for r in result.candidates] == [
        ("Shared Group",),
        ("Shared Group",),
    ]


def test_render_rechecks_hidden_or_unmerged_person(directory_case):
    c = directory_case
    a = c.add_person("Visible")
    b = c.add_person("Alias", merged_into=a)
    c.set_roster("g@g.us", [a, c.counterpart, c.assistant])
    reader = c.reader("group")
    assert render_person(b, reader).person_id == a
    c.writer.execute("UPDATE persons SET merged_into=NULL WHERE id=?", (b,))
    with pytest.raises(PeopleUnavailable, match="^people_out_of_scope$"):
        render_person(b, reader)
    c.add_name(a, "Observed")
    assert "Observed" in render_person(a, reader).names
    c.writer.execute("UPDATE person_names SET hidden=1 WHERE name='Observed'")
    assert "Observed" not in render_person(a, reader).names
    c.set_roster("g@g.us", [c.counterpart, c.assistant], at_ms=200)
    with pytest.raises(PeopleUnavailable, match="^people_out_of_scope$"):
        render_person(a, c.reader("group"))


def test_language_never_serialized(directory_case, detailed_person):
    assert "preferred_language" not in asdict(
        render_person(detailed_person, directory_case.reader())
    )


def test_unknown_roster_only_authors_get_names(directory_case):
    c = directory_case
    p = c.add_person("Former", ids=(("phone", "4915000000011"),))
    c.add_author("g@g.us", p, at_ms=100)
    reader = c.reader("group")
    assert render_person(p, reader).phones == ()
    with pytest.raises(PeopleUnavailable):
        render_person(c.owner, reader)


def test_owner_dm_builder_requires_authentication_and_current_snapshot(directory_case):
    c = directory_case
    kwargs = dict(
        channel="whatsapp",
        chat_id=c.endpoint(c.owner),
        principal_id="whatsapp:" + c.endpoint(c.owner),
        at_ms=300,
        history=c.snapshot,
    )
    assert reader_context(c.directory, is_owner=True, **kwargs).kind == "owner_dm"
    assert reader_context(c.directory, is_owner=False, **kwargs).kind == "other_dm"
    with pytest.raises(HistoryPaused):
        reader_context(c.directory, is_owner=True, **(kwargs | {"history": None}))
    kwargs["history"].close()
    with pytest.raises(HistoryPaused):
        reader_context(c.directory, is_owner=True, **kwargs)


@pytest.mark.parametrize(
    "owner_phone,expected",
    [
        ("4915000000099", "49"),
        ("14155550123", "1"),
        ("447700900123", "44"),
        ("49garbage", None),
        ("+4915000000099", None),
    ],
)
def test_national_calling_code_comes_from_directory_owner(directory_case, owner_phone, expected):
    c = directory_case
    c.writer.execute(
        "UPDATE person_ids SET value=? WHERE person_id=? AND kind='phone'", (owner_phone, c.owner)
    )
    reader = reader_context(
        c.directory,
        channel="whatsapp",
        chat_id=c.endpoint(c.counterpart),
        principal_id="whatsapp:" + c.endpoint(c.counterpart),
        is_owner=False,
        at_ms=300,
        history=c.snapshot,
        national_calling_code="99",
    )
    assert reader.national_calling_code == expected
    c.writer.execute(
        "UPDATE person_ids SET hidden=1 WHERE person_id=? AND kind='phone'", (c.owner,)
    )
    assert (
        reader_context(
            c.directory,
            channel=reader.channel,
            chat_id=reader.chat_id,
            principal_id=reader.principal_id,
            is_owner=False,
            at_ms=300,
            history=c.snapshot,
        ).national_calling_code
        is None
    )


def test_ambiguous_owner_phone_has_no_calling_code(directory_case):
    c = directory_case
    c.add_id(c.owner, "phone", "447700900123")
    reader = reader_context(
        c.directory,
        channel="whatsapp",
        chat_id=c.endpoint(c.owner),
        principal_id="whatsapp:" + c.endpoint(c.owner),
        is_owner=True,
        at_ms=300,
        history=c.snapshot,
    )
    assert reader.national_calling_code is None


def test_non_whatsapp_owner_requires_directory_proven_endpoint(directory_case):
    c = directory_case
    c.add_id(c.owner, "telegram_id", "1234567")
    kwargs = dict(
        channel="telegram",
        chat_id="1234567",
        principal_id="telegram:1234567",
        is_owner=True,
        at_ms=300,
        history=None,
    )
    assert reader_context(c.directory, **kwargs).kind == "owner_dm"
    other = reader_context(c.directory, **(kwargs | {"chat_id": "7654321"}))
    assert other.kind == "other_dm"
    assert c.directory.scope(other).eligible_ids == frozenset()
    c.add_id(c.counterpart, "telegram_id", "7654321")
    assert (
        render_person(c.counterpart, replace(other, principal_id="telegram:7654321")).phones == ()
    )


def test_render_seen_exact_and_unnamed_fallback(directory_case):
    c = directory_case
    p = c.add_person("Quiet Person", source="observed", level="seen")
    assert render_person(p, c.reader()).display_name == "Quiet Person"
    c.writer.execute("UPDATE person_names SET hidden=1 WHERE person_id=?", (p,))
    assert render_person(p, c.reader()).display_name == "Unnamed person"


def test_non_whatsapp_unverified_binding_has_empty_scope(directory_case):
    c = directory_case
    c.add_id(c.counterpart, "telegram_id", "7654321")
    reader = reader_context(
        c.directory,
        channel="telegram",
        chat_id="7654321",
        principal_id="telegram:1234567",
        is_owner=True,
        at_ms=300,
        history=None,
    )
    assert reader.kind == "other_dm"
    assert c.directory.scope(reader).eligible_ids == frozenset()


def test_malformed_public_identifier_cannot_encode_a_phone(directory_case):
    c = directory_case
    p = c.add_person("Subject", ids=(("wa_lid", "4915000000011@s.whatsapp.net"),))
    c.add_author("g@g.us", p, at_ms=100)
    # Independent valid LID evidence provides recognition, not phone disclosure.
    c.add_id(p, "wa_lid", "300000000001")
    c.hdb.execute("UPDATE messages SET sender_identifier='300000000001@lid'")
    reader = c.reader("group")
    assert resolve_person("Subject", reader).person_id == p
    assert render_person(p, reader).identifiers == (("wa_lid", "300000000001"),)


def test_telegram_group_and_phone_are_not_direct_owner_endpoints(directory_case):
    c = directory_case
    c.add_id(c.owner, "telegram_id", "1234567")
    base = dict(
        channel="telegram", principal_id="telegram:1234567", is_owner=True, at_ms=300, history=None
    )
    group = reader_context(c.directory, chat_id="-1001234567", **base)
    assert group.kind == "group"
    assert c.directory.scope(group).eligible_ids == frozenset()
    phone = reader_context(c.directory, chat_id="4915000000099", **base)
    assert phone.kind == "other_dm"
    assert c.directory.scope(phone).eligible_ids == frozenset()


def test_ambiguous_telegram_endpoint_does_not_grant_scope(directory_case):
    c = directory_case
    c.add_id(c.owner, "telegram_id", "1234567", end=400)
    c.add_id(c.counterpart, "telegram_id", "1234567", start=200)
    reader = reader_context(
        c.directory,
        channel="telegram",
        chat_id="1234567",
        principal_id="telegram:1234567",
        is_owner=True,
        at_ms=300,
        history=None,
    )
    assert reader.kind == "other_dm"
    assert c.directory.scope(reader).eligible_ids == frozenset()
