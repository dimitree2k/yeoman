"""The public Knowledge compatibility surface shares directory recognition."""

from dataclasses import replace

import pytest
from yeoman_gateway.history.context import history_knowledge_scope
from yeoman_gateway.knowledge.models import TrustedReadContext
from yeoman_gateway.processing.tool_context import (
    ToolInvocationContext,
    reset_tool_context,
    set_tool_context,
)


@pytest.fixture
def knowledge_directory_case(directory_case, tmp_path):
    from tests.gateway.test_contacts_tool import selected_history

    c = directory_case
    fixture = selected_history.__wrapped__(tmp_path)
    _, c.service = next(fixture)
    c.service.people = c.directory
    c.subject = c.add_person("Vincent", ids=(("phone", "4915000000011"),))
    c.add_id(c.subject, "knowledge_person", "legacy-a")
    c.add_name(c.subject, "Owner Nickname", source="owner")
    c.set_roster("g@g.us", [c.owner, c.counterpart, c.assistant, c.subject])
    try:
        yield c
    finally:
        fixture.close()


def context(c, kind="owner_dm"):
    r = c.reader(kind)
    return TrustedReadContext(
        r.principal_id.removesuffix("@s.whatsapp.net"),
        r.channel,
        r.chat_id,
        (
            frozenset({"whatsapp:4915000000098", "whatsapp:4915000000011"})
            if kind == "group"
            else frozenset({r.principal_id.removesuffix("@s.whatsapp.net")})
        ),
        "synthetic",
        c.service.policy_revision,
        "reply",
        r.at_ms,
        is_direct=kind != "group",
        owner=kind == "owner_dm",
    ), r.history


def search(c, name, kind="owner_dm"):
    ctx, snapshot = context(c, kind)
    with history_knowledge_scope(snapshot, c.service):
        return c.service.search_people(name, context=ctx)


def test_knowledge_search_uses_directory_alias(knowledge_directory_case):
    c = knowledge_directory_case
    before = tuple(c.service._store.connection.iterdump())
    result = search(c, "Owner Nickname")
    assert {(r.person_id, r.status) for r in result} == {
        ("legacy-a", "ambiguous"),
        ("4", "ambiguous"),
    }
    assert {r.display_name for r in result} == {"Vincent"}
    assert tuple(c.service._store.connection.iterdump()) == before


def test_knowledge_bridge_conflict_and_frozen_tables(knowledge_directory_case):
    c = knowledge_directory_case
    c.add_id(c.subject, "knowledge_person", "private-fixture")
    c.add_person("Different", ids=(("phone", "4917632625469"),))
    before = tuple(c.service._store.connection.iterdump())
    result = search(c, "Owner Nickname")
    assert tuple(c.service._store.connection.iterdump()) == before
    assert "private-fixture" not in {r.person_id for r in result}


def test_unmapped_candidate_does_not_collapse_ambiguity(knowledge_directory_case):
    c = knowledge_directory_case
    c.add_person("Vincent")
    results = search(c, "Vincent")
    assert results and all(r.status == "ambiguous" for r in results)


def test_knowledge_search_never_matches_substrings(knowledge_directory_case):
    assert search(knowledge_directory_case, "incen") == ()


def test_search_policy_cannot_promote_group_to_owner(knowledge_directory_case):
    c = knowledge_directory_case
    c.add_person("PRIVATE Outside")
    ctx, snapshot = context(c, "group")
    with history_knowledge_scope(snapshot, c.service):
        token = set_tool_context(
            ToolInvocationContext(
                channel=ctx.channel,
                chat_id=ctx.chat_id,
                canonical_user_id=ctx.principal_id,
                is_owner=True,
                history_snapshot=snapshot,
                knowledge_read_context=ctx,
            )
        )
        try:
            assert c.service.search_people_with_policy("PRIVATE Outside") == ()
            assert c.service.person_reader(replace(ctx, owner=True)).kind == "group"
        finally:
            reset_tool_context(token)
        assert c.service.search_people_with_policy("Vincent") == ()


def test_knowledge_candidate_methods_share_directory_and_offered_scope(knowledge_directory_case):
    c = knowledge_directory_case
    ctx, snapshot = context(c, "group")
    with history_knowledge_scope(snapshot, c.service):
        for result in (
            c.service.search_mention_name_candidates("Owner Nickname", context=ctx),
            c.service.search_mention_text_candidates("Ask Owner Nickname today", context=ctx),
        ):
            assert {r.person_id for r in result} == {"4"}
            assert all(r.status == "ambiguous" for r in result)
            assert all(r.reason == "name_candidate_requires_confirmation" for r in result)
        restricted = replace(ctx, recipient_principals=frozenset({ctx.principal_id}))
        assert c.service.search_mention_name_candidates("Vincent", context=restricted) == ()


def test_missing_directory_does_not_use_private_matcher(knowledge_directory_case):
    c = knowledge_directory_case
    c.service.people = None
    assert search(c, "Vincent") == ()


def test_unique_legacy_subject_keeps_resolved_status(knowledge_directory_case):
    c = knowledge_directory_case
    c.writer.execute(
        "UPDATE person_ids SET hidden=1 WHERE kind='knowledge_person' AND value='legacy-a'"
    )
    results = search(c, "Owner Nickname")
    assert [(r.person_id, r.status) for r in results] == [("4", "resolved")]


@pytest.fixture
def large_knowledge_case(tmp_path):
    from .test_resolver_perf import real_size_directory

    yield from real_size_directory.__wrapped__(tmp_path)


@pytest.mark.perf
def test_public_search_real_size_warm_latency(large_knowledge_case, tmp_path):
    import json
    import time

    from tests.gateway.test_contacts_tool import selected_history

    from .test_resolver_perf import p95

    c = large_knowledge_case
    root = tmp_path / "knowledge"
    root.mkdir()
    fixture = selected_history.__wrapped__(root)
    _, c.service = next(fixture)
    c.service.people = c.directory
    try:
        ctx, snapshot = context(c)
        with history_knowledge_scope(snapshot, c.service):
            expected = c.service.search_people("Person0004", context=ctx)
            assert expected
            samples = []
            for _ in range(30):
                start = time.perf_counter_ns()
                assert c.service.search_people("Person0004", context=ctx) == expected
                samples.append((time.perf_counter_ns() - start) / 1e6)
            print(json.dumps({"public_search_p95_ms": p95(samples)}), flush=True)
            assert p95(samples) <= 20
    finally:
        fixture.close()
