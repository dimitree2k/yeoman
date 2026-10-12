"""Tests for the contacts LLM tool."""

import json
import time
from pathlib import Path

import pytest
from yeoman_gateway.agent.tools.contacts import ContactsTool
from yeoman_gateway.agent.tools.resolve_contact import (
    ResolveContactTool,
    resolve_contact_reference,
)
from yeoman_gateway.knowledge._contacts.service import ContactsService

from tests.gateway.history_test_helpers import (
    ProjectedHistory,
    prepare_history_retirement_knowledge,
)

_OWNER = "4915000000000@s.whatsapp.net"


@pytest.fixture
def contacts(tmp_path: Path) -> ContactsService:
    return ContactsService(db_path=tmp_path / "contacts.db")


@pytest.fixture
def tool(contacts: ContactsService) -> ContactsTool:
    t = ContactsTool(knowledge=None)
    t.set_context(channel="whatsapp", chat_id="test@g.us")
    return t


@pytest.fixture
def selected_history(tmp_path: Path):
    from yeoman_gateway.knowledge.api import open_knowledge_store
    from yeoman_gateway.knowledge.authority import FakeSourceAuthority
    from yeoman_gateway.knowledge.runtime import RuntimeKnowledgePolicy

    policy = RuntimeKnowledgePolicy(
        engine=None,
        admin_principals=frozenset({f"whatsapp:{_OWNER}"}),
    )
    authority = FakeSourceAuthority()
    knowledge = open_knowledge_store(
        prepare_history_retirement_knowledge(tmp_path, "selected"),
        workspace_id="contacts-selected-tests",
        source_authority=authority,
        policy_authority=policy,
    )
    history = ProjectedHistory(tmp_path / "selected-history")
    try:
        yield history, knowledge
    finally:
        history.close()
        knowledge.close()


async def _execute_with_history(history: ProjectedHistory, tool: object, **kwargs: object) -> str:
    from yeoman_gateway.processing.tool_context import (
        ToolInvocationContext,
        reset_tool_context,
        set_tool_context,
    )

    async with history.turn() as snapshot:
        token = set_tool_context(ToolInvocationContext(
            channel="whatsapp", chat_id="test@g.us", history_snapshot=snapshot,
        ))
        try:
            return await tool.execute(**kwargs)
        finally:
            reset_tool_context(token)


async def _execute_selected(
    history: ProjectedHistory, knowledge: object, tool: object, **kwargs: object
) -> str:
    from yeoman_gateway.history.context import history_knowledge_scope
    from yeoman_gateway.processing.tool_context import (
        ToolInvocationContext,
        reset_tool_context,
        set_tool_context,
    )

    async with history.turn() as snapshot:
        with history_knowledge_scope(snapshot, knowledge):
            token = set_tool_context(ToolInvocationContext(
                channel="whatsapp", chat_id="test@g.us", history_snapshot=snapshot,
            ))
            try:
                return await tool.execute(**kwargs)
            finally:
                reset_tool_context(token)


def _add_contacts_tool_person(
    history: ProjectedHistory, knowledge: object, *, phone: str, name: str
) -> None:
    timestamp = time.time_ns() // 1_000_000 - 1000
    history.add_contact(identifiers=[phone], name=name, timestamp=timestamp)


def _add_person(history: ProjectedHistory, *, phone: str, name: str, lid: str | None = None) -> None:
    timestamp = time.time_ns() // 1_000_000 - 1000
    history.add_contact(identifiers=[_OWNER], name="Synthetic owner", timestamp=timestamp)
    identifiers = [phone, *([lid] if lid else [])]
    history.add_contact(identifiers=identifiers, name=name, timestamp=timestamp)
    participants = [_OWNER, {"lid": lid, "phoneJid": phone}] if lid else [_OWNER, phone]
    history.add_group(
        chat_id="finance@g.us",
        participants=participants,
        timestamp=timestamp,
        name="Finanzgruppe",
    )


async def _resolve(history: ProjectedHistory, knowledge: object, resolver: ResolveContactTool, query: str) -> str:
    from yeoman_gateway.history.context import history_knowledge_scope
    from yeoman_gateway.processing.tool_context import (
        ToolInvocationContext,
        reset_tool_context,
        set_tool_context,
    )

    async with history.turn() as snapshot:
        with history_knowledge_scope(snapshot, knowledge):
            token = set_tool_context(ToolInvocationContext(
                channel="whatsapp", chat_id="finance@g.us", history_snapshot=snapshot,
            ))
            try:
                return await resolver.execute(query=query)
            finally:
                reset_tool_context(token)


class TestContactsTool:


    @pytest.mark.asyncio
    async def test_search(self, selected_history) -> None:
        history, knowledge = selected_history
        phone = "4915000000001@s.whatsapp.net"
        _add_contacts_tool_person(history, knowledge, phone=phone, name="Alex")
        tool = ContactsTool(knowledge=knowledge)
        tool.set_context("whatsapp", "test@g.us")
        result = await _execute_selected(history, knowledge, tool, action="search", query="Alex")
        assert "Alex" in result

    @pytest.mark.asyncio
    async def test_get_info(self, selected_history) -> None:
        history, knowledge = selected_history
        phone = "4915000000002@s.whatsapp.net"
        _add_contacts_tool_person(history, knowledge, phone=phone, name="Alex")
        tool = ContactsTool(knowledge=knowledge)
        tool.set_context("whatsapp", "test@g.us")
        result = await _execute_selected(history, knowledge, tool, action="get", name="Alex")
        assert "Alex" in result


    @pytest.mark.asyncio
    async def test_tool_schema(self, tool: ContactsTool) -> None:
        schema = tool.to_schema()
        assert schema["function"]["name"] == "contacts"
        assert "action" in schema["function"]["parameters"]["properties"]


class TestResolveContactTool:
    def test_resolver_without_knowledge_fails_closed(self) -> None:
        assert resolve_contact_reference(

            knowledge=None,
            reference="Alex",
            channel="whatsapp",
            chat_id="test@g.us",
        ) is None

    @pytest.mark.asyncio
    async def test_resolves_partial_name_without_disclosing_fields(
        self,
        contacts: ContactsService,
        selected_history,
    ) -> None:
        history, knowledge = selected_history
        phone = "4917632625469@s.whatsapp.net"
        contact_id = contacts.ensure_contact(
            channel="whatsapp",
            identifier=phone,
            kind="phone_jid",
            push_name="Frank Taeger",
        )
        contacts.store.add_field(
            contact_id=contact_id,
            kind="note",
            value="sensitive personal note",
        )
        # A proven platform observation is what makes this person resolvable; the legacy
        # row above only proves the tool never reads its private fields.
        _add_person(history, phone=phone, name="Frank Taeger")
        resolver = ResolveContactTool(knowledge=knowledge)
        resolver.set_context(channel="whatsapp", chat_id="finance@g.us")

        result = await _resolve(history, knowledge, resolver, "Frank")

        payload = json.loads(result)
        assert payload["ok"] is True
        assert payload["contact"]["display_name"] == "Frank Taeger"
        assert payload["contact"]["jid"] == "4917632625469@s.whatsapp.net"
        assert "sensitive personal note" not in result

    @pytest.mark.asyncio
    async def test_group_resolution_requires_current_projected_roster_membership(
        self, selected_history,
    ) -> None:
        history, knowledge = selected_history
        member = "4917632625469@s.whatsapp.net"
        outsider = "4917000000000@s.whatsapp.net"
        _add_person(history, phone=member, name="Current Member")
        timestamp = time.time_ns() // 1_000_000 - 500
        history.add_contact(identifiers=[outsider], name="Former Member", timestamp=timestamp)
        # Make the outsider a scoped historical author without placing them in the
        # latest complete roster. Historical authorship alone must not authorize a send.
        history.add_message(
            chat_id="finance@g.us",
            message_id="former-member-message",
            text="older group message",
            timestamp=timestamp + 100,
            sender_id=outsider,
            participant=outsider,
            sender_name="Former Member",
        )
        resolver = ResolveContactTool(knowledge=knowledge)
        resolver.set_context(channel="whatsapp", chat_id="finance@g.us")

        member_result = json.loads(
            await _resolve(history, knowledge, resolver, "Current Member")
        )
        outsider_name = json.loads(
            await _resolve(history, knowledge, resolver, "Former Member")
        )
        outsider_mention = json.loads(
            await _resolve(history, knowledge, resolver, "@4917000000000")
        )

        assert member_result["ok"] is True
        assert member_result["contact"]["jid"] == member
        assert outsider_name["ok"] is False
        assert outsider_name["error_code"] == "contact_not_resolved"
        assert outsider_mention["ok"] is False
        assert outsider_mention["error_code"] == "contact_not_resolved"

    @pytest.mark.asyncio
    async def test_an_unproven_legacy_contact_is_not_a_delivery_target(
        self,
        contacts: ContactsService,
        selected_history,
    ) -> None:
        """Knowledge is the sole authority: the legacy cache is not a second opinion.

        The WhatsApp resolver never falls back to the legacy cache when Knowledge is wired.
        """
        history, knowledge = selected_history
        contacts.ensure_contact(
            channel="whatsapp",
            identifier="4917632625469@s.whatsapp.net",
            kind="phone_jid",
            push_name="Frank Taeger",
        )
        resolver = ResolveContactTool(knowledge=knowledge)
        resolver.set_context(channel="whatsapp", chat_id="finance@g.us")

        payload = json.loads(await _resolve(history, knowledge, resolver, "Frank"))
        assert payload["ok"] is False
        assert payload["error_code"] == "contact_not_resolved"
        # The selected reader has no legacy-cache-only resolution path.

    @pytest.mark.asyncio
    async def test_resolves_lid_mention_to_phone_jid_in_current_group(
        self,
        contacts: ContactsService,
        selected_history,
    ) -> None:
        history, knowledge = selected_history
        phone = "4917632625469@s.whatsapp.net"
        lid = "46918273106072@lid"
        contacts.ensure_contact(
            channel="whatsapp",
            identifier=phone,
            kind="phone_jid",
            push_name="Frank Taeger",
        )
        _add_person(history, phone=phone, lid=lid, name="Frank Taeger")
        resolver = ResolveContactTool(knowledge=knowledge)
        resolver.set_context(channel="whatsapp", chat_id="finance@g.us")

        result = await _resolve(history, knowledge, resolver, "@46918273106072")

        payload = json.loads(result)
        assert payload["contact"]["display_name"] == "Frank Taeger"
        assert payload["contact"]["jid"] == "4917632625469@s.whatsapp.net"
        assert payload["contact"]["matched_identifier"] == "46918273106072@lid"

    @pytest.mark.asyncio
    async def test_name_resolution_does_not_use_substring_only_match(
        self, contacts: ContactsService, selected_history,
    ) -> None:
        history, knowledge = selected_history
        phone = "4917000000000@s.whatsapp.net"
        contacts.ensure_contact(
            channel="whatsapp",
            identifier=phone,
            kind="phone_jid",
            push_name="Joanne Miller",
        )
        _add_person(history, phone=phone, name="Joanne Miller")
        resolver = ResolveContactTool(knowledge=knowledge)
        resolver.set_context(
            channel="whatsapp",
            chat_id="substring@g.us",
        )

        payload = json.loads(await _resolve(history, knowledge, resolver, "Ann"))
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
        selected_history,
        query: str,
    ) -> None:
        history, knowledge = selected_history
        phone = "4917632625469@s.whatsapp.net"
        contacts.ensure_contact(
            channel="whatsapp",
            identifier=phone,
            kind="phone_jid",
            push_name="Frank Taeger",
        )
        resolver = ResolveContactTool(knowledge=knowledge)
        resolver.set_context(channel="whatsapp", chat_id="finance@g.us")

        _add_person(history, phone=phone, name="Frank Taeger")
        result = await _resolve(history, knowledge, resolver, query)

        payload = json.loads(result)
        assert payload["ok"] is False
        assert payload["error_code"] == "contact_not_resolved"
