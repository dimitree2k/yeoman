"""Tests for the contacts LLM tool."""

import pytest
from pathlib import Path

from yeoman.contacts.service import ContactsService
from yeoman.agent.tools.contacts import ContactsTool


@pytest.fixture
def contacts(tmp_path: Path) -> ContactsService:
    return ContactsService(db_path=tmp_path / "contacts.db")


@pytest.fixture
def tool(contacts: ContactsService) -> ContactsTool:
    t = ContactsTool(contacts)
    t.set_context(channel="whatsapp", chat_id="test@g.us")
    return t


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
