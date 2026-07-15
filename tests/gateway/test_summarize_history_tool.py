from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from yeoman_gateway.agent.tools.media_history import MediaHistoryTool
from yeoman_gateway.agent.tools.summarize_history import SummarizeHistoryTool
from yeoman_gateway.contacts.service import ContactsService
from yeoman_gateway.media.document_cache import DocumentCache
from yeoman_gateway.storage.inbound_archive import InboundArchive


@pytest.fixture
def archive(tmp_path):
    return InboundArchive(db_path=tmp_path / "test.db")


@pytest.fixture
def contacts(tmp_path):
    svc = ContactsService(db_path=tmp_path / "contacts.db")
    cid = svc.ensure_contact(
        channel="whatsapp",
        identifier="4912345678901@s.whatsapp.net",
        kind="phone_jid",
        push_name="Alice",
    )
    svc.update_display_name(cid, "Alice")
    return svc


def _seed(archive, base_ts, texts, sender_id="4912345678901@s.whatsapp.net", sender_name="Alice"):
    for i, text in enumerate(texts):
        archive.record_inbound(
            channel="whatsapp",
            chat_id="group@g.us",
            message_id=f"m-{i}",
            participant=None,
            sender_id=sender_id,
            text=text,
            timestamp=base_ts + i * 60,
            sender_name=sender_name,
        )


def test_hides_group_parameter_outside_owner_dm(archive) -> None:
    tool = SummarizeHistoryTool(archive, None)
    tool.set_context("whatsapp", "group@g.us", is_owner=True)

    function = tool.to_schema()["function"]

    assert "owner DM" not in function["description"]
    assert "different chat" not in function["description"]
    assert "group" not in function["parameters"]["properties"]


def test_exposes_group_parameter_for_owner_dm(archive) -> None:
    tool = SummarizeHistoryTool(archive, None)
    tool.set_context("whatsapp", "491757070305@s.whatsapp.net", is_owner=True)

    function = tool.to_schema()["function"]

    assert "owner DM" in function["description"]
    assert "group" in function["parameters"]["properties"]


def test_media_history_tool_hides_group_parameter_outside_owner_dm(tmp_path: Path) -> None:
    tool = MediaHistoryTool(
        cache=DocumentCache(tmp_path / "document_cache.db"),
        processor=None,
        group_resolver=lambda ref: ("finanzgruppe@g.us", None),
    )
    tool.set_context("whatsapp", "group@g.us", is_owner=True)

    function = tool.to_schema()["function"]

    assert "owner DM" not in function["description"]
    assert "another WhatsApp group" not in function["description"]
    assert "group" not in function["parameters"]["properties"]


def test_media_history_tool_exposes_group_parameter_for_owner_dm(tmp_path: Path) -> None:
    tool = MediaHistoryTool(
        cache=DocumentCache(tmp_path / "document_cache.db"),
        processor=None,
        group_resolver=lambda ref: ("finanzgruppe@g.us", None),
    )
    tool.set_context("whatsapp", "owner@lid", is_owner=True)

    function = tool.to_schema()["function"]

    assert "owner DM" in function["description"]
    assert "group" in function["parameters"]["properties"]


@pytest.mark.asyncio
async def test_returns_formatted_messages(archive, contacts) -> None:
    base_ts = int(datetime.now(UTC).timestamp()) - 600
    _seed(archive, base_ts, ["hello", "world"])

    tool = SummarizeHistoryTool(archive, contacts)
    tool.set_context("whatsapp", "group@g.us")

    result = await tool.execute(hours_back=1)
    assert "Alice" in result
    assert "hello" in result
    assert "world" in result


@pytest.mark.asyncio
async def test_resolves_mention_tokens(archive, contacts) -> None:
    base_ts = int(datetime.now(UTC).timestamp()) - 600
    _seed(archive, base_ts, ["hey @4912345678901 check this"])

    tool = SummarizeHistoryTool(archive, contacts)
    tool.set_context("whatsapp", "group@g.us")

    result = await tool.execute(hours_back=1)
    assert "@Alice" in result
    assert "@4912345678901" not in result


@pytest.mark.asyncio
async def test_returns_empty_message_when_no_history(archive) -> None:
    tool = SummarizeHistoryTool(archive, None)
    tool.set_context("whatsapp", "group@g.us")

    result = await tool.execute(hours_back=1)
    assert "No messages found" in result


@pytest.mark.asyncio
async def test_defaults_to_today(archive) -> None:
    base_ts = int(datetime.now(UTC).timestamp()) - 300
    _seed(archive, base_ts, ["recent msg"])

    tool = SummarizeHistoryTool(archive, None)
    tool.set_context("whatsapp", "group@g.us")

    result = await tool.execute()
    assert "recent msg" in result
