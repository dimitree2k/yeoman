"""Composition shares a lazy directory; private recognizers are retired."""

from types import SimpleNamespace

import pytest
from yeoman_gateway.people.resolver import PeopleResolver, PeopleUnavailable


def test_history_private_person_resolvers_removed():
    from yeoman_gateway.history.queries import HistoryQueries
    from yeoman_gateway.knowledge._history_identity import HistoryIdentityEngine
    from yeoman_gateway.knowledge._person_identity import IdentityEngine

    assert not {
        "resolve_person",
        "_eligible_person_names",
        "resolve_people_in_text",
        "person_names",
    } & set(vars(HistoryQueries))
    assert "search_by_name" not in vars(HistoryIdentityEngine)
    assert not {
        "search_mention_name_candidates",
        "search_mention_text_candidates",
        "person_by_display_or_alias",
    } & set(vars(IdentityEngine))


@pytest.fixture(params=[True, False])
def runtime_case(tmp_path, monkeypatch, request):
    from yeoman_gateway.app import bootstrap
    from yeoman_gateway.people.schema import open_people_db

    from tests.gateway.test_person_consumer_cutover import (
        test_the_knowledge_composition_does_not_hand_the_legacy_cache_to_consumers,
    )

    path = tmp_path / "synthetic-people.db"
    if request.param:
        open_people_db(path).close()
    monkeypatch.setattr(bootstrap, "people_db_path", lambda **kwargs: path)
    captured = []
    original = bootstrap.build_gateway_runtime

    def build(**kwargs):
        runtime = original(**kwargs)
        captured.append(runtime)
        return runtime

    monkeypatch.setattr(bootstrap, "build_gateway_runtime", build)
    test_the_knowledge_composition_does_not_hand_the_legacy_cache_to_consumers(
        tmp_path, monkeypatch
    )
    runtime = captured[0]
    try:
        yield runtime, path
    finally:
        if getattr(runtime, "people", None) is not None:
            runtime.people.close()
        runtime.responder.knowledge.close()


def test_composition_injects_same_directory(runtime_case):
    runtime, path = runtime_case
    assert isinstance(runtime.people, PeopleResolver)
    assert runtime.people_sync.people is runtime.people
    assert runtime.responder.people is runtime.people
    assert runtime.responder.knowledge.people is runtime.people
    assert runtime.responder._recall_tool._people is runtime.people
    for name in ("contacts", "resolve_contact"):
        assert runtime.responder.tools.get(name)._people is runtime.people
    assert not hasattr(runtime.responder, "contacts_service")
    if not path.exists():
        with pytest.raises(PeopleUnavailable):
            runtime.people._index()
        assert not path.exists()


async def test_missing_directory_tools_degrade_without_losing_the_turn(
    runtime_case, directory_case, monkeypatch
):
    from yeoman_gateway.processing.tool_context import (
        ToolInvocationContext,
        reset_tool_context,
        set_tool_context,
    )

    runtime, path = runtime_case
    missing = PeopleResolver(path.parent / "absent.db")
    c = directory_case
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
        contacts = runtime.responder.tools.get("contacts")
        contacts._people = missing
        assert "unavailable" in await contacts.execute(action="get", name="Synthetic Owner")
        recall = runtime.responder._recall_tool
        recall._people = missing
        assert "unavailable" in await recall.execute(person="Synthetic Owner", query="hello")
        import json

        resolver = runtime.responder.tools.get("resolve_contact")
        resolver._people = missing
        assert json.loads(await resolver.execute(query="Synthetic Owner"))["ok"] is False
        runtime.responder.people = missing
        assert (
            runtime.responder._resolve_pending_delivery_target(
                pending={"target_name": "Synthetic Owner"},
                content="@4915000000099",
                metadata={},
                channel=reader.channel,
                chat_id=reader.chat_id,
            )
            is None
        )
        from yeoman_gateway.history.context import history_knowledge_scope

        monkeypatch.setattr(
            "yeoman_gateway.history.context.current_history_snapshot", lambda: reader.history
        )
        with history_knowledge_scope(reader.history, runtime.responder.knowledge):
            assert (
                runtime.responder._knowledge_statement_context(
                    query="What do you know about Synthetic Owner?",
                    channel=reader.channel,
                    chat_id=reader.chat_id,
                    principal=reader.principal_id,
                    is_owner=True,
                )
                == ""
            )
        assert not missing.db_path.exists()
    finally:
        reset_tool_context(token)
        missing.close()


async def test_shutdown_closes_directory_after_users_stop(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock, Mock

    from yeoman_gateway.app import bootstrap
    from yeoman_gateway.people.schema import open_people_db

    path = tmp_path / "people.db"
    open_people_db(path).close()
    directory = PeopleResolver(path)
    directory._index()
    connection = directory._local.state.connection
    order = []
    original = directory.close

    def close():
        order.append("people")
        original()

    monkeypatch.setattr(directory, "close", close)
    monkeypatch.setattr(bootstrap.tracing, "init", lambda: None)
    monkeypatch.setattr(bootstrap.tracing, "shutdown", AsyncMock())
    runtime = bootstrap.GatewayRuntime(
        orchestrator=SimpleNamespace(
            run=AsyncMock(), stop=Mock(side_effect=lambda: order.append("users"))
        ),
        channels=SimpleNamespace(start_all=AsyncMock(), stop_all=AsyncMock()),
        cron=SimpleNamespace(start=AsyncMock(), stop=Mock()),
        heartbeat=SimpleNamespace(start=AsyncMock(), stop=Mock()),
        responder=SimpleNamespace(aclose=AsyncMock()),
        history_projector=SimpleNamespace(start=AsyncMock(), stop=AsyncMock()),
        people=directory,
    )
    monkeypatch.setattr(bootstrap.GatewayRuntime, "_start_processing_services", AsyncMock())
    monkeypatch.setattr(bootstrap.GatewayRuntime, "_resume_a2a_research", lambda self: None)
    await runtime.run()
    assert order == ["users", "people"]
    import sqlite3

    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")
