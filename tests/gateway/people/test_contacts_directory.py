"""Directory recognition, disclosure and legacy subject bridge contracts."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from yeoman_gateway.agent.tools.contacts import ContactsTool
from yeoman_gateway.knowledge.models import PersonProfileView, TrustedReadContext
from yeoman_gateway.processing.tool_context import (
    ToolInvocationContext,
    reset_tool_context,
    set_tool_context,
)


@pytest.fixture
def contacts_directory_case(directory_case):
    c = directory_case
    c.subject = c.add_person("Vincent", ids=(("phone", "4915000000011"),))
    c.add_name(c.subject, "Owner Nickname", source="owner")
    c.writer.execute("UPDATE persons SET notes='PRIVATE_notes' WHERE id=?", (c.subject,))
    c.set_roster("g@g.us", [c.owner, c.counterpart, c.assistant, c.subject])
    c.knowledge = SimpleNamespace(
        directory_subject_ids=Mock(return_value=("legacy-a",)),
        person_profile=Mock(return_value=PersonProfileView(card="note: allowed statement")),
        search_people_with_policy=Mock(side_effect=AssertionError("legacy recognition")),
        owner_read_context=Mock(side_effect=AssertionError("manufactured owner rights")),
        record_note=Mock(return_value=SimpleNamespace(changed_ids=("statement",))),
        erase_matching_statements=Mock(return_value=1),
    )
    c.tool = ContactsTool(knowledge=c.knowledge, people=c.directory)
    return c


async def execute(c, reader_kind="owner_dm", *, read_context=True, **args):
    reader = c.reader(reader_kind)
    context = (
        TrustedReadContext(
            principal_id=reader.principal_id,
            channel=reader.channel,
            chat_id=reader.chat_id,
            recipient_principals=frozenset({reader.principal_id}),
            owner=reader_kind == "owner_dm",
            membership_revision="synthetic",
            policy_revision=1,
            now_ms=reader.at_ms,
            is_direct=reader_kind != "group",
            purpose="reply",
        )
        if read_context
        else None
    )
    token = set_tool_context(
        ToolInvocationContext(
            channel=reader.channel,
            chat_id=reader.chat_id,
            canonical_user_id=reader.principal_id,
            is_owner=reader_kind == "owner_dm",
            history_snapshot=reader.history,
            knowledge_read_context=context,
        )
    )
    try:
        return await c.tool.execute(**args)
    finally:
        reset_tool_context(token)


@pytest.mark.parametrize("kind", ["group", "other_dm"])
async def test_contacts_outputs_use_disclosure(contacts_directory_case, kind):
    c = contacts_directory_case
    output = await execute(c, kind, action="get", name="Vincent")
    assert "Name: Vincent" in output and "PRIVATE_" not in output
    if kind == "other_dm":
        assert "4915000000011" not in output
    context = c.knowledge.person_profile.call_args.kwargs["context"]
    assert context.owner is False
    c.knowledge.search_people_with_policy.assert_not_called()


@pytest.mark.parametrize("action", ["add_field", "remove_field"])
async def test_contacts_write_requires_one_legacy_subject(contacts_directory_case, action):
    c = contacts_directory_case
    c.knowledge.directory_subject_ids.return_value = ("legacy-a", "legacy-b")
    output = await execute(c, action=action, name="Vincent", kind="note", value="new statement")
    assert output.startswith("Error:") and "ambig" in output.lower()
    c.knowledge.record_note.assert_not_called()
    c.knowledge.erase_matching_statements.assert_not_called()


async def test_contacts_directory_only_person_is_readable_but_not_writable(contacts_directory_case):
    c = contacts_directory_case
    c.knowledge.directory_subject_ids.return_value = ()
    assert "Name: Vincent" in await execute(c, action="get", name="Owner Nickname")
    assert "Person:" not in await execute(c, action="get", name="Vincent")
    output = await execute(c, action="add_field", name="Vincent", kind="note", value="note")
    assert output.startswith("Error:") and "unavailable" in output
    c.knowledge.record_note.assert_not_called()


async def test_contacts_not_found_never_falls_back_to_knowledge(contacts_directory_case):
    c = contacts_directory_case
    assert "No people found" in await execute(c, action="search", query="Absent")
    c.knowledge.search_people_with_policy.assert_not_called()


@pytest.mark.parametrize("action", ["get", "remove_field"])
async def test_same_name_is_ambiguous(contacts_directory_case, action):
    c = contacts_directory_case
    c.add_person("Vincent")
    output = await execute(c, action=action, name="Vincent", kind="note", value="note")
    assert output.startswith("Error:") and "matches 2 people" in output
    c.knowledge.person_profile.assert_not_called()
    c.knowledge.erase_matching_statements.assert_not_called()


async def test_no_read_context_only_renders_directory(contacts_directory_case):
    c = contacts_directory_case
    output = await execute(c, action="get", name="Vincent", read_context=False)
    assert "Name: Vincent" in output and "allowed statement" not in output
    c.knowledge.person_profile.assert_not_called()


async def test_profiles_deduplicate_and_never_supply_directory_metadata(contacts_directory_case):
    c = contacts_directory_case
    c.knowledge.directory_subject_ids.return_value = ("legacy-a", "legacy-b")
    c.knowledge.person_profile.return_value = PersonProfileView(
        card="name: PRIVATE_old\naliases: PRIVATE_alias\ncontact: PRIVATE_id\nnote: statement"
    )
    output = await execute(c, action="get", name="Vincent")
    assert "PRIVATE_old" not in output and "PRIVATE_alias" not in output
    assert "PRIVATE_id" not in output and "Person:" not in output
    assert output.count("note: statement") == 1
    assert c.knowledge.person_profile.call_count == 2


def test_frozen_subject_bridge_is_exact_read_only_and_conflict_safe(directory_case, tmp_path):
    from tests.gateway.test_contacts_tool import selected_history

    fixture = selected_history.__wrapped__(tmp_path)
    _, knowledge = next(fixture)
    c = directory_case
    p = c.add_person("Directory Preferred", ids=(("phone", "4917632625469"),))
    try:
        before = tuple(knowledge._store.connection.iterdump())
        assert knowledge.directory_subject_ids(p, reader=c.reader()) == tuple(
            sorted(("private-fixture", str(p)))
        )
        assert tuple(knowledge._store.connection.iterdump()) == before
        # Explicit provenance cannot override positive identifier ownership.
        other = c.add_person("Different", ids=(("phone", "4915000000088"),))
        c.add_id(other, "knowledge_person", "private-fixture")
        assert "private-fixture" not in knowledge.directory_subject_ids(other, reader=c.reader())
    finally:
        fixture.close()


async def test_unavailable_legacy_profile_is_not_reported_as_directory_content(
    contacts_directory_case,
):
    c = contacts_directory_case
    c.knowledge.person_profile.return_value = PersonProfileView(reason="unknown_person")
    output = await execute(c, action="get", name="Vincent")
    assert "Name: Vincent" in output and "Knowledge content unavailable" in output


async def test_unique_legacy_mutations_keep_bounded_public_calls(contacts_directory_case):
    c = contacts_directory_case
    added = await execute(c, action="add_field", name="Owner Nickname", kind="note", value="note")
    assert added.startswith("Added note")
    assert c.knowledge.record_note.call_args.args == ("legacy-a",)
    removed = await execute(
        c, action="remove_field", name="Owner Nickname", kind="note", value="note"
    )
    assert removed.endswith("(1 statements)")
    assert c.knowledge.erase_matching_statements.call_args.args == ("legacy-a",)
    assert c.knowledge.erase_matching_statements.call_args.kwargs["contains"] == "note"


async def test_contacts_renders_at_most_eight_ambiguity_candidates(
    contacts_directory_case,
    monkeypatch,
):
    from yeoman_gateway.people import disclosure

    c = contacts_directory_case
    for _ in range(10):
        c.add_person("Same Name")
    rendered = []
    original = disclosure.render_person

    def tracked(person, reader):
        rendered.append(person)
        return original(person, reader)

    monkeypatch.setattr(disclosure, "render_person", tracked)
    output = await execute(c, action="search", query="Same Name")
    assert len(rendered) == 8
    assert output.count("Same Name") == 8 and "truncated" in output


async def test_list_owner_dm_uses_contact_scope_and_disclosure_only(contacts_directory_case):
    c = contacts_directory_case
    c.add_person("PRIVATE_seen", source="observed", level="seen")
    c.add_person("Curated Seen", level="seen")  # Owner name makes effective level curated.
    c.add_id(c.subject, "phone", "4915000000012", hidden=True)
    c.add_id(c.subject, "phone", "4915000000013", end=1)
    c.add_id(c.subject, "phone", "4915000000014")
    other = c.add_person("Conflicting Phone")
    c.add_id(other, "phone", "4915000000014", end=9999999999999)
    output = await execute(c, action="list")
    assert "- Vincent (phone:4915000000011)" in output
    assert "Curated Seen" in output
    assert "PRIVATE_" not in output
    assert "4915000000012" not in output and "4915000000013" not in output
    assert "4915000000014" not in output
    assert "Owner Nickname" not in output and "allowed statement" not in output
    c.knowledge.directory_subject_ids.assert_not_called()
    c.knowledge.person_profile.assert_not_called()


@pytest.mark.parametrize("kind", ["group", "other_dm"])
async def test_list_denied_outside_owner_dm(contacts_directory_case, kind):
    c = contacts_directory_case
    output = await execute(c, kind, action="list")
    assert output == "Error: listing contacts is only available in the owner DM"
    assert "Vincent" not in output
    c.knowledge.directory_subject_ids.assert_not_called()


async def test_list_pagination_order_and_merged_terminals(contacts_directory_case):
    c = contacts_directory_case
    first = c.add_person("Alpha", ids=(("phone", "4915000000021"),))
    c.add_person("Alpha", ids=(("phone", "4915000000022"),))
    middle = c.add_person("Middle Alias", merged_into=first)
    c.add_person("Earlier Alias", merged_into=middle)
    for n in reversed(range(55)):
        c.add_person(f"Contact {n:02}")
    output = await execute(c, action="list")
    lines = [line for line in output.splitlines() if line.startswith("- ")]
    assert len(lines) == 25
    assert lines[:2] == ["- Alpha (phone:4915000000021)", "- Alpha (phone:4915000000022)"]
    assert lines[2] == "- Contact 00 ()"
    assert "Next offset: 25" in output
    page = await execute(c, action="list", offset=25, limit=50)
    page_lines = [line for line in page.splitlines() if line.startswith("- ")]
    assert page_lines[0] == "- Contact 23 ()"
    assert len(page_lines) == 36
    assert "Next offset:" not in page
    assert "Alias" not in output + page
    max_page = await execute(c, action="list", limit=50)
    assert len([line for line in max_page.splitlines() if line.startswith("- ")]) == 50
    assert await execute(c, action="list", offset=1000) == "No contacts found."


@pytest.mark.parametrize(
    "args",
    [
        {"limit": 0},
        {"limit": -1},
        {"limit": 51},
        {"limit": "25"},
        {"limit": True},
        {"limit": None},
        {"limit": 1.5},
        {"offset": -1},
        {"offset": "0"},
        {"offset": True},
        {"offset": None},
        {"offset": 1.5},
    ],
)
async def test_list_invalid_pagination_is_rejected(contacts_directory_case, args):
    output = await execute(contacts_directory_case, action="list", **args)
    assert output.startswith("Error: list needs")


async def test_list_empty_contact_scope(contacts_directory_case):
    c = contacts_directory_case
    c.writer.execute("UPDATE persons SET preferred_name=NULL, notes=NULL, level='seen'")
    c.writer.execute("DELETE FROM person_names")
    assert await execute(c, action="list") == "No contacts found."
