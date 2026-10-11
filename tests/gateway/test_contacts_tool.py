"""Read-only Contacts tool tests backed by the Knowledge facade."""

from pathlib import Path

import pytest
from yeoman_gateway.agent.tools.contacts import ContactsTool


@pytest.fixture
def knowledge(tmp_path: Path):
    from tests.gateway.knowledge.conftest import KnowledgeHarness

    harness = KnowledgeHarness(tmp_path)
    principal = "whatsapp:10001"
    harness.policy.admins = {principal}
    harness.policy.owners = {principal}
    harness.policy.admin_actor = lambda: principal
    harness.person_for(principal, "Alex Example")
    context = harness.service.owner_read_context(channel="whatsapp", chat_id="test@g.us")
    harness.policy.set_members(context, {principal}, revision="test")
    try:
        yield harness.service
    finally:
        harness.close()


@pytest.fixture
def tool(knowledge) -> ContactsTool:
    result = ContactsTool(knowledge=knowledge)
    result.set_context(channel="whatsapp", chat_id="test@g.us")
    return result


class TestContactsTool:
    @pytest.mark.asyncio
    async def test_search(self, tool: ContactsTool) -> None:
        result = await tool.execute(action="search", query="Alex")
        assert "Alex Example" in result

    @pytest.mark.asyncio
    async def test_get(self, tool: ContactsTool) -> None:
        result = await tool.execute(action="get", name="Alex Example")
        assert "Alex Example" in result
        assert "10001@s.whatsapp.net" in result

    def test_schema_exposes_only_read_actions(self, tool: ContactsTool) -> None:
        schema = tool.to_schema()
        assert schema["function"]["name"] == "contacts"
        actions = schema["function"]["parameters"]["properties"]["action"]["enum"]
        assert actions == ["search", "get"]
