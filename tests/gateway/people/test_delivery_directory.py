"""Recognition never substitutes for endpoint, roster or disclosure proof."""

import json
from types import SimpleNamespace

import pytest
from yeoman_gateway.agent.tools.resolve_contact import (
    ContactResolution,
    ResolveContactTool,
    contact_resolution_matches_reference,
    resolve_contact_reference,
)
from yeoman_gateway.people.resolver import resolve_person
from yeoman_gateway.processing.tool_context import (
    ToolInvocationContext,
    reset_tool_context,
    set_tool_context,
)


@pytest.fixture
def delivery_directory_case(directory_case):
    c = directory_case
    c.subject = c.add_person(
        "Vincent", ids=(("phone", "4915000000011"), ("wa_lid", "300000000011"))
    )
    c.phone_jid = "4915000000011@s.whatsapp.net"
    c.add_name(c.subject, "Owner Nickname", source="owner")
    c.set_roster("g@g.us", [c.owner, c.counterpart, c.assistant, c.subject])
    c.tool = ResolveContactTool(people=c.directory)
    return c


async def resolve(c, query, kind="group"):
    reader = c.reader(kind)
    token = set_tool_context(
        ToolInvocationContext(
            channel=reader.channel,
            chat_id=reader.chat_id,
            canonical_user_id=reader.principal_id,
            is_owner=kind == "owner_dm",
            history_snapshot=reader.history,
        )
    )
    try:
        return json.loads(await c.tool.execute(query=query))
    finally:
        reset_tool_context(token)


async def test_former_author_is_recognizable_but_not_deliverable(delivery_directory_case):
    c = delivery_directory_case
    c.add_author("g@g.us", c.subject, at_ms=100)
    c.set_roster("g@g.us", [c.owner, c.counterpart, c.assistant], at_ms=200)
    assert resolve_person("Vincent", c.reader("group")).status == "resolved"
    assert await resolve(c, "Vincent") == {
        "ok": False,
        "error_code": "contact_not_resolved",
        "query": "Vincent",
    }


async def test_delivery_alias_uses_one_resolver_and_proven_binding(delivery_directory_case):
    c = delivery_directory_case
    payload = await resolve(c, "Owner Nickname")
    assert payload["contact"] == {
        "display_name": "Vincent",
        "jid": c.phone_jid,
        "matched_identifier": None,
    }
    c.hdb.execute("DELETE FROM identifier_history WHERE contact_id=?", (str(c.subject),))
    assert (await resolve(c, "Owner Nickname"))["ok"] is False


async def test_other_dm_delivery_result_never_exposes_phone(delivery_directory_case):
    c = delivery_directory_case
    payload = await resolve(c, "Vincent", "other_dm")
    assert payload["ok"] is False and c.phone_jid not in json.dumps(payload)


async def test_lid_mention_preserves_matched_identifier(delivery_directory_case):
    c = delivery_directory_case
    payload = await resolve(c, "@300000000011")
    assert payload["contact"]["jid"] == c.phone_jid
    assert payload["contact"]["matched_identifier"] == "300000000011@lid"


async def test_two_active_phones_are_unresolved(delivery_directory_case):
    c = delivery_directory_case
    c.add_id(c.subject, "phone", "4915000000012")
    assert (await resolve(c, "Vincent"))["ok"] is False


async def test_directory_owner_id_without_transport_proof(delivery_directory_case):
    c = delivery_directory_case
    c.hdb.execute("DELETE FROM identifier_history WHERE contact_id=?", (str(c.subject),))
    assert (await resolve(c, "Owner Nickname", "owner_dm"))["ok"] is False


async def test_current_number_cannot_bypass_conflicting_terminal(delivery_directory_case):
    c = delivery_directory_case
    c.writer.execute(
        "UPDATE person_ids SET valid_until_ms=200 WHERE person_id=? AND kind='phone'",
        (c.subject,),
    )
    other = c.add_person("Other Holder")
    c.add_id(other, "phone", "4915000000011", start=200)
    assert (await resolve(c, "+4915000000011", "owner_dm"))["ok"] is False


async def test_ambiguous_name_cannot_be_confirmed_by_mention(delivery_directory_case):
    c = delivery_directory_case
    other = c.add_person("Vincent", ids=(("phone", "4915000000012"),))
    c.set_roster("g@g.us", [c.owner, c.counterpart, c.subject, other])
    reader = c.reader("group")
    assert not contact_resolution_matches_reference(
        reference="Vincent",
        resolution=ContactResolution("Vincent", c.phone_jid),
        people=c.directory,
        reader=reader,
    )
    from yeoman_gateway.adapters.responder_llm import LLMResponder

    responder = LLMResponder.__new__(LLMResponder)
    responder.people = c.directory
    responder.knowledge = None
    token = set_tool_context(
        ToolInvocationContext(
            channel=reader.channel,
            chat_id=reader.chat_id,
            canonical_user_id=reader.principal_id,
            history_snapshot=reader.history,
        )
    )
    try:
        assert (
            responder._resolve_pending_delivery_target(
                pending={"target_name": "Vincent"},
                content="@300000000011",
                metadata={"mentioned_jids": ["300000000011@lid"]},
                channel=reader.channel,
                chat_id=reader.chat_id,
            )
            is None
        )
    finally:
        reset_tool_context(token)


def test_retired_generation_fences_delivery(delivery_directory_case):
    from yeoman_gateway.history.live import HistoryPaused

    c = delivery_directory_case
    reader = c.reader("group")
    reader.history.close()
    with pytest.raises(HistoryPaused):
        resolve_contact_reference(
            reference="Vincent",
            channel=reader.channel,
            chat_id=reader.chat_id,
            people=c.directory,
            reader=reader,
        )


def test_missing_context_or_directory_never_uses_legacy_recognizer(delivery_directory_case):
    c = delivery_directory_case
    poisoned = SimpleNamespace(search_people_with_policy=lambda *a, **k: pytest.fail("legacy"))
    assert (
        resolve_contact_reference(
            reference="Vincent", channel="whatsapp", chat_id="g@g.us", knowledge=poisoned
        )
        is None
    )
    assert (
        resolve_contact_reference(
            reference="Vincent", channel="whatsapp", chat_id="g@g.us", people=c.directory
        )
        is None
    )


def test_pending_confirmation_reuses_one_trusted_reader(delivery_directory_case, monkeypatch):
    import yeoman_gateway.adapters.responder_llm as module
    from yeoman_gateway.adapters.responder_llm import LLMResponder

    c = delivery_directory_case
    responder = LLMResponder.__new__(LLMResponder)
    responder.people = c.directory
    responder.knowledge = None
    reader = c.reader("group")
    calls = []
    original = module.resolve_contact_reference

    def tracked(**kwargs):
        calls.append(kwargs["reader"])
        return original(**kwargs)

    monkeypatch.setattr(module, "resolve_contact_reference", tracked)
    token = set_tool_context(
        ToolInvocationContext(
            channel=reader.channel,
            chat_id=reader.chat_id,
            canonical_user_id=reader.principal_id,
            history_snapshot=reader.history,
        )
    )
    try:
        assert (
            responder._resolve_pending_delivery_target(
                pending={"target_name": "Owner Nickname"},
                content="@300000000011",
                metadata={"mentioned_jids": ["300000000011@lid"]},
                channel=reader.channel,
                chat_id=reader.chat_id,
            )
            == c.phone_jid
        )
        assert len(calls) == 2 and calls[0] is calls[1]
        assert calls[0].principal_id == reader.principal_id
    finally:
        reset_tool_context(token)


async def test_invalidated_generation_never_sends_pending_delivery(delivery_directory_case):
    from unittest.mock import AsyncMock, Mock

    from yeoman_gateway.adapters.responder_llm import LLMResponder
    from yeoman_gateway.history.live import HistoryPaused

    c = delivery_directory_case
    reader = c.reader("group")
    responder = LLMResponder.__new__(LLMResponder)
    responder.people = c.directory
    responder.knowledge = None
    responder.tools = SimpleNamespace(execute=AsyncMock())
    pending = {
        "tool": "send_voice",
        "target_name": "Vincent",
        "content": "synthetic voice",
        "initiating_sender_aliases": [c.endpoint(c.counterpart)],
    }
    session = SimpleNamespace(metadata={"pending_delivery": pending}, add_tool_call=Mock())
    token = set_tool_context(
        ToolInvocationContext(
            channel=reader.channel,
            chat_id=reader.chat_id,
            canonical_user_id=reader.principal_id,
            history_snapshot=reader.history,
        )
    )
    reader.history.close()
    try:
        with pytest.raises(HistoryPaused):
            await responder._maybe_complete_pending_delivery(
                session=session,
                content="@300000000011",
                metadata={"sender_id": c.endpoint(c.counterpart)},
                allowed_tools={"send_voice"},
                channel=reader.channel,
                chat_id=reader.chat_id,
            )
        responder.tools.execute.assert_not_called()
    finally:
        reset_tool_context(token)


@pytest.fixture
def large_delivery_case(tmp_path):
    from .test_resolver_perf import real_size_directory

    yield from real_size_directory.__wrapped__(tmp_path)


@pytest.mark.perf
def test_delivery_and_subject_bridge_reuse_real_size_scope(large_delivery_case, tmp_path):
    import time
    from dataclasses import replace

    from tests.gateway.test_contacts_tool import selected_history

    from .test_resolver_perf import p95

    c = large_delivery_case
    reader = replace(c.reader("group"), chat_id="g0@g.us")
    root = tmp_path / "knowledge"
    root.mkdir()
    fixture = selected_history.__wrapped__(root)
    _, knowledge = next(fixture)
    try:
        start = time.perf_counter_ns()
        found = resolve_contact_reference(
            reference="Person0004",
            channel=reader.channel,
            chat_id=reader.chat_id,
            people=c.directory,
            reader=reader,
        )
        cold = (time.perf_counter_ns() - start) / 1e6
        assert found is not None and found.jid == c.endpoint(4)
        knowledge.directory_subject_ids(4, reader=reader)
        samples, bridges = [], []
        for _ in range(30):
            start = time.perf_counter_ns()
            assert (
                resolve_contact_reference(
                    reference="Person0004",
                    channel=reader.channel,
                    chat_id=reader.chat_id,
                    people=c.directory,
                    reader=reader,
                )
                == found
            )
            samples.append((time.perf_counter_ns() - start) / 1e6)
            start = time.perf_counter_ns()
            assert "4" in knowledge.directory_subject_ids(4, reader=reader)
            bridges.append((time.perf_counter_ns() - start) / 1e6)
        report = {
            "delivery_cold_ms": cold,
            "delivery_p95_ms": p95(samples),
            "subject_bridge_p95_ms": p95(bridges),
        }
        print(json.dumps(report), flush=True)
        assert report["delivery_p95_ms"] <= 20
        assert report["subject_bridge_p95_ms"] <= 20
    finally:
        fixture.close()
