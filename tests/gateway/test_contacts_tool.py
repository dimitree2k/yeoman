"""Tests for the contacts LLM tool."""

import json
from pathlib import Path

import pytest
from yeoman_gateway.agent.tools.contacts import ContactsTool
from yeoman_gateway.agent.tools.resolve_contact import ResolveContactTool
from yeoman_gateway.contacts.service import ContactsService
from yeoman_gateway.storage.chat_registry import ChatRegistry


@pytest.fixture
def contacts(tmp_path: Path) -> ContactsService:
    return ContactsService(db_path=tmp_path / "contacts.db")


@pytest.fixture
def knowledge(contacts: ContactsService, tmp_path: Path):
    """Public person-knowledge facade with a live delegate onto the legacy contacts.

    The tests keep writing people through the legacy service (that is what the runtime
    did before the migration).  The knowledge store promotes each unknown identifier on
    first lookup, so the facade and the legacy store stay consistent without the tests
    reaching into either store.
    """
    from yeoman_gateway.knowledge.api import open_knowledge_store
    from yeoman_gateway.knowledge.authority import (
        FakePolicyAuthority,
        FakeSourceAuthority,
        PolicyMembership,
    )

    class _PromotingPolicy(FakePolicyAuthority):
        def admin_actor(self) -> str:
            return "whatsapp:owner"

        def membership(self, context):
            return PolicyMembership(members=frozenset({context.principal_id}), revision="test")

    service = open_knowledge_store(
        tmp_path / "knowledge.db",
        workspace_id="contacts-tool-tests",
        source_authority=FakeSourceAuthority(),
        policy_authority=_PromotingPolicy(
            admins={"whatsapp:owner"}, capture_actors={"whatsapp:owner"}
        ),
    )

    def promote(contacts_service):
        for person_id in sorted(set(contacts_service.known_jids.values())):
            row = contacts_service.store.get_contact(person_id)
            if row is None:
                continue
            service.promote_legacy_person(
                person_id=person_id,
                display_name=row.display_name,
                identifiers=tuple(contacts_service.store.get_identifiers(person_id)),
                aliases=tuple(contacts_service.store.get_aliases(person_id)),
                fields=tuple(contacts_service.store.get_fields(person_id)),
            )
        for identifier, contact_id in list(contacts_service.known_jids.items()):
            row = contacts_service.store.get_contact(contact_id)
            if row is None:
                continue
            service.promote_legacy_person(
                person_id=contact_id,
                display_name=row.display_name,
                identifiers=tuple(contacts_service.store.get_identifiers(contact_id)),
                aliases=tuple(contacts_service.store.get_aliases(contact_id)),
                fields=tuple(contacts_service.store.get_fields(contact_id)),
            )
    promote(contacts)
    yield service
    service.close()


def _promote(knowledge, contacts: ContactsService, *extra_identifiers: str) -> None:
    """Re-run the legacy-to-knowledge promotion after a test created new people.

    ``extra_identifiers`` are synthetic tokens that a test used directly; they are
    registered so identifier lookups can find the person without a name guess.
    """
    for person_id in sorted(set(contacts.known_jids.values())):
        row = contacts.store.get_contact(person_id)
        if row is None:
            continue
        knowledge.promote_legacy_person(person_id=person_id, display_name=row.display_name)
    if extra_identifiers:
        for person_id in {value for value in contacts.known_jids.values()}:
            for token in extra_identifiers:
                if contacts.known_jids.get(token) != person_id:
                    continue
                knowledge.bind_identifier_for_migration(
                    person_id=person_id,
                    channel="whatsapp",
                    kind="phone_jid",
                    value=token,
                )
    for identifier, contact_id in list(contacts.known_jids.items()):
        row = contacts.store.get_contact(contact_id)
        if row is None:
            continue
        knowledge.promote_legacy_person(
            person_id=contact_id,
            display_name=row.display_name,
            identifiers=tuple(contacts.store.get_identifiers(contact_id)),
            aliases=tuple(contacts.store.get_aliases(contact_id)),
            fields=tuple(contacts.store.get_fields(contact_id)),
        )


@pytest.fixture
def tool(contacts: ContactsService, knowledge: object) -> ContactsTool:
    t = ContactsTool(contacts, knowledge=knowledge)
    t.set_context(channel="whatsapp", chat_id="test@g.us")
    return t


@pytest.fixture
def chat_registry(tmp_path: Path) -> ChatRegistry:
    registry = ChatRegistry(db_path=tmp_path / "chat_registry.db")
    registry.register_chat(
        channel="whatsapp",
        chat_id="finance@g.us",
        chat_type="group",
        readable_name="Finanzgruppe",
        metadata={
            "participants": [
                {
                    "id": "46918273106072@lid",
                    "phoneNumber": "4917632625469@s.whatsapp.net",
                }
            ]
        },
    )
    return registry


class TestContactsTool:
    @pytest.mark.asyncio
    async def test_update_name(
        self, tool: ContactsTool, contacts: ContactsService, knowledge: object
    ) -> None:
        contacts.ensure_contact(
            channel="whatsapp", identifier="jid1", kind="phone_jid", push_name="Unknown",
        )
        _promote(knowledge, contacts, "jid1")
        result = await tool.execute(action="update_name", identifier="jid1", name="Alex")
        assert "Alex" in result
        assert tool._knowledge.search_people_with_policy("Alex")

    @pytest.mark.asyncio
    async def test_add_field(
        self, tool: ContactsTool, contacts: ContactsService, knowledge: object
    ) -> None:
        contacts.ensure_contact(
            channel="whatsapp", identifier="jid1", kind="phone_jid", push_name="Alex",
        )
        _promote(knowledge, contacts)
        result = await tool.execute(
            action="add_field", name="Alex", kind="email",
            value="alex@bmw.de", label="work",
        )
        assert "email" in result.lower()

    @pytest.mark.asyncio
    async def test_search(
        self, tool: ContactsTool, contacts: ContactsService, knowledge: object
    ) -> None:
        contacts.ensure_contact(
            channel="whatsapp", identifier="jid1", kind="phone_jid", push_name="Alex",
        )
        _promote(knowledge, contacts)
        result = await tool.execute(action="search", query="Alex")
        assert "Alex" in result

    @pytest.mark.asyncio
    async def test_get_info(
        self, tool: ContactsTool, contacts: ContactsService, knowledge: object
    ) -> None:
        contacts.ensure_contact(
            channel="whatsapp", identifier="jid1", kind="phone_jid", push_name="Alex",
        )
        _promote(knowledge, contacts)
        result = await tool.execute(action="get", name="Alex")
        assert "Alex" in result

    @pytest.mark.asyncio
    async def test_merge(
        self, tool: ContactsTool, contacts: ContactsService, knowledge: object
    ) -> None:
        contacts.ensure_contact(
            channel="whatsapp", identifier="jid1", kind="phone_jid", push_name="Alex",
        )
        contacts.ensure_contact(
            channel="whatsapp", identifier="jid2", kind="phone_jid", push_name="Unknown",
        )
        _promote(knowledge, contacts)
        result = await tool.execute(action="merge", target_name="Alex", source_name="Unknown")
        assert "merged" in result.lower() or "Merged" in result

    @pytest.mark.asyncio
    async def test_tool_schema(self, tool: ContactsTool) -> None:
        schema = tool.to_schema()
        assert schema["function"]["name"] == "contacts"
        assert "action" in schema["function"]["parameters"]["properties"]


class TestResolveContactTool:
    @pytest.mark.asyncio
    async def test_resolves_partial_name_without_disclosing_fields(
        self, contacts: ContactsService, chat_registry: ChatRegistry
    ) -> None:
        contact_id = contacts.ensure_contact(
            channel="whatsapp",
            identifier="4917632625469@s.whatsapp.net",
            kind="phone_jid",
            push_name="Frank Taeger",
        )
        contacts.store.add_field(
            contact_id=contact_id,
            kind="note",
            value="sensitive personal note",
        )
        resolver = ResolveContactTool(contacts=contacts, knowledge=knowledge, chat_registry=chat_registry)
        resolver.set_context(channel="whatsapp", chat_id="finance@g.us")

        result = await resolver.execute(query="Frank")

        payload = json.loads(result)
        assert payload["ok"] is True
        assert payload["contact"]["display_name"] == "Frank Taeger"
        assert payload["contact"]["jid"] == "4917632625469@s.whatsapp.net"
        assert "sensitive personal note" not in result

    @pytest.mark.asyncio
    async def test_resolves_lid_mention_to_phone_jid_in_current_group(
        self,
        contacts: ContactsService,
        chat_registry: ChatRegistry,
        knowledge: object,
    ) -> None:
        contacts.ensure_contact(
            channel="whatsapp",
            identifier="4917632625469@s.whatsapp.net",
            kind="phone_jid",
            push_name="Frank Taeger",
        )
        _promote(knowledge, contacts)
        resolver = ResolveContactTool(contacts=contacts, knowledge=knowledge, chat_registry=chat_registry)
        resolver.set_context(channel="whatsapp", chat_id="finance@g.us")

        result = await resolver.execute(query="@46918273106072")

        payload = json.loads(result)
        assert payload["contact"]["display_name"] == "Frank Taeger"
        assert payload["contact"]["jid"] == "4917632625469@s.whatsapp.net"
        assert payload["contact"]["matched_identifier"] == "46918273106072@lid"

    @pytest.mark.asyncio
    async def test_name_resolution_does_not_use_substring_only_match(
        self,
        contacts: ContactsService,
        tmp_path: Path,
    ) -> None:
        contacts.ensure_contact(
            channel="whatsapp",
            identifier="4917000000000@s.whatsapp.net",
            kind="phone_jid",
            push_name="Joanne Miller",
        )
        registry = ChatRegistry(db_path=tmp_path / "substring_registry.db")
        registry.register_chat(
            channel="whatsapp",
            chat_id="substring@g.us",
            chat_type="group",
            readable_name="Substring",
            metadata={
                "participants": [
                    {
                        "id": "10000000000000@lid",
                        "phoneNumber": "4917000000000@s.whatsapp.net",
                    }
                ]
            },
        )
        resolver = ResolveContactTool(
            contacts=contacts,
            chat_registry=registry,
        )
        resolver.set_context(
            channel="whatsapp",
            chat_id="substring@g.us",
        )

        result = await resolver.execute(query="Ann")

        registry.close()
        payload = json.loads(result)
        assert payload["ok"] is False
        assert payload["error_code"] == "contact_not_resolved"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "query",
        [
            "x4917632625469y",
            "Frank4917632625469evil",
            "4917632625469@evil",
            "@4917632625469@evil",
            "4917632625469@s.whatsapp.net.evil",
            "4917632625469＠evil",
        ],
    )
    async def test_identifier_resolution_requires_token_boundaries(
        self,
        contacts: ContactsService,
        chat_registry: ChatRegistry,
        query: str,
    ) -> None:
        contacts.ensure_contact(
            channel="whatsapp",
            identifier="4917632625469@s.whatsapp.net",
            kind="phone_jid",
            push_name="Frank Taeger",
        )
        resolver = ResolveContactTool(
            contacts=contacts,
            knowledge=knowledge,
            chat_registry=chat_registry,
        )
        resolver.set_context(channel="whatsapp", chat_id="finance@g.us")

        result = await resolver.execute(query=query)

        payload = json.loads(result)
        assert payload["ok"] is False
        assert payload["error_code"] == "contact_not_resolved"
