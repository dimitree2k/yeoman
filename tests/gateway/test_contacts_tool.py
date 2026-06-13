"""Tests for the contacts LLM tool."""

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
def tool(contacts: ContactsService) -> ContactsTool:
    t = ContactsTool(contacts)
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
    async def test_update_name(self, tool: ContactsTool, contacts: ContactsService) -> None:
        contacts.ensure_contact(
            channel="whatsapp", identifier="jid1", kind="phone_jid", push_name="Unknown",
        )
        result = await tool.execute(action="update_name", identifier="jid1", name="Alex")
        assert "Alex" in result
        cid = contacts.known_jids["jid1"]
        assert contacts.store.get_contact(cid).display_name == "Alex"

    @pytest.mark.asyncio
    async def test_add_field(self, tool: ContactsTool, contacts: ContactsService) -> None:
        contacts.ensure_contact(
            channel="whatsapp", identifier="jid1", kind="phone_jid", push_name="Alex",
        )
        result = await tool.execute(
            action="add_field", name="Alex", kind="email",
            value="alex@bmw.de", label="work",
        )
        assert "email" in result.lower()

    @pytest.mark.asyncio
    async def test_search(self, tool: ContactsTool, contacts: ContactsService) -> None:
        contacts.ensure_contact(
            channel="whatsapp", identifier="jid1", kind="phone_jid", push_name="Alex",
        )
        result = await tool.execute(action="search", query="Alex")
        assert "Alex" in result

    @pytest.mark.asyncio
    async def test_get_info(self, tool: ContactsTool, contacts: ContactsService) -> None:
        contacts.ensure_contact(
            channel="whatsapp", identifier="jid1", kind="phone_jid", push_name="Alex",
        )
        contacts.store.add_field(
            contact_id=contacts.known_jids["jid1"],
            kind="email", value="alex@test.com",
        )
        result = await tool.execute(action="get", name="Alex")
        assert "alex@test.com" in result

    @pytest.mark.asyncio
    async def test_merge(self, tool: ContactsTool, contacts: ContactsService) -> None:
        contacts.ensure_contact(
            channel="whatsapp", identifier="jid1", kind="phone_jid", push_name="Alex",
        )
        contacts.ensure_contact(
            channel="whatsapp", identifier="jid2", kind="phone_jid", push_name="Unknown",
        )
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
        resolver = ResolveContactTool(contacts=contacts, chat_registry=chat_registry)
        resolver.set_context(channel="whatsapp", chat_id="finance@g.us")

        result = await resolver.execute(query="Frank")

        assert "Resolved contact: Frank Taeger" in result
        assert "4917632625469@s.whatsapp.net" in result
        assert "sensitive personal note" not in result

    @pytest.mark.asyncio
    async def test_resolves_lid_mention_to_phone_jid_in_current_group(
        self, contacts: ContactsService, chat_registry: ChatRegistry
    ) -> None:
        contacts.ensure_contact(
            channel="whatsapp",
            identifier="4917632625469@s.whatsapp.net",
            kind="phone_jid",
            push_name="Frank Taeger",
        )
        resolver = ResolveContactTool(contacts=contacts, chat_registry=chat_registry)
        resolver.set_context(channel="whatsapp", chat_id="finance@g.us")

        result = await resolver.execute(query="@46918273106072")

        assert "Resolved contact: Frank Taeger" in result
        assert "4917632625469@s.whatsapp.net" in result
        assert "46918273106072@lid" in result
