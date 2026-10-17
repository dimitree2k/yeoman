from yeoman_gateway.agent.tools.recall_conversation import RecallConversationTool
from yeoman_gateway.processing.tool_context import (
    ToolInvocationContext,
    reset_tool_context,
    set_tool_context,
)


async def execute(c, tool, **args):
    reader = c.reader()
    token = set_tool_context(
        ToolInvocationContext(
            channel=reader.channel,
            chat_id=reader.chat_id,
            canonical_user_id=reader.principal_id,
            is_owner=True,
            history_snapshot=reader.history,
        )
    )
    try:
        return await tool.execute(**args)
    finally:
        reset_tool_context(token)


async def test_recall_uses_directory_alias_not_history_name(directory_case):
    c = directory_case
    p = c.add_person("Directory Preferred", ids=(("phone", "4915000000011"),))
    c.add_name(p, "Owner Nickname", source="owner")
    c.set_roster("g@g.us", [c.owner, p], label="Shared")
    c.add_message("authored", sender_person=p, identifier=c.endpoint(p), at_ms=100, text="holdings")
    c.hdb.execute(
        "UPDATE contacts SET display_name='Different Archive Name' WHERE contact_id=?", (str(p),)
    )
    tool = RecallConversationTool(None, people=c.directory)
    result = await execute(c, tool, query="holdings", person="Owner Nickname")
    assert "holdings" in result and "sender=Directory Preferred" in result
    assert "PRIVATE_" not in result


async def test_directory_recognition_does_not_grant_history_source_permission(directory_case):
    c = directory_case
    c.add_person("Directory Only")
    tool = RecallConversationTool(None, people=c.directory)
    assert (
        await execute(c, tool, query="holdings", person="Directory Only")
        == "No matching messages found for 'holdings'."
    )


async def test_missing_directory_never_uses_retired_recognition(directory_case, monkeypatch):
    from yeoman_gateway.history.queries import HistoryQueries

    c = directory_case

    def poisoned(*args, **kwargs):
        raise AssertionError("old resolver")

    monkeypatch.setattr(HistoryQueries, "resolve_person", poisoned)
    assert "unavailable" in await execute(
        c, RecallConversationTool(None), query="holdings", person="Anything"
    )


async def test_ambiguous_cards_are_filtered_and_truncated(directory_case):
    c = directory_case
    people = [c.add_person("Same Person") for _ in range(10)]
    c.set_roster("g@g.us", [c.owner, *people], label="Shared")
    c.set_roster("private@g.us", people, label="Private Group")
    tool = RecallConversationTool(None, people=c.directory)
    result = await execute(c, tool, query="holdings", person="Same Person")
    assert "Several people match" in result and result.count("Same Person (groups:") == 8
    assert "truncated" in result and "Private Group" not in result
