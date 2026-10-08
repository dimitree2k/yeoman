import json
import os
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from yeoman_gateway.adapters.policy_engine import EnginePolicyAdapter
from yeoman_gateway.adapters.responder_llm import LLMResponder
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.core.admin_commands import AdminCommandContext
from yeoman_gateway.history.live import HistoryPaused
from yeoman_gateway.history.queries import HistoryQueries
from yeoman_gateway.history.reader import HistorySnapshot
from yeoman_gateway.history.schema import FTS_ROWS, create
from yeoman_gateway.policy.engine import PolicyEngine
from yeoman_gateway.policy.schema import PolicyConfig
from yeoman_gateway.processing.responder import ThreadActorResponder
from yeoman_gateway.providers.base import LLMProvider, LLMResponse
from yeoman_gateway.session.manager import SessionManager
from yeoman_shared.utils.helpers import get_operational_store_path, get_sessions_path


@pytest.fixture
def case(tmp_path):
    from yeoman_gateway.session.operational import OperationalSessions

    db = sqlite3.connect(":memory:")
    create(db)
    for mid, ms, text in (("undated", None, "older"), ("old", 10, "older"),
                          ("equal", 20, "older"), ("new", 30, "current")):
        db.execute(
            "INSERT INTO messages VALUES (?, 'whatsapp', 'dm:synthetic', ?, NULL, NULL,"
            " 'unknown', 'in', ?, 'native', ?, NULL, NULL, NULL, 'native', '[]')",
            (mid, mid, ms, text),
        )
    db.execute("INSERT INTO messages_fts(message_id,chat_id,text) " + FTS_ROWS)
    snapshot = HistorySnapshot(1, (), db)
    path = get_operational_store_path("session_metadata", data_dir=tmp_path, create=False)
    ops = OperationalSessions(path)
    sessions = SessionManager(tmp_path, sessions_dir=tmp_path / "sessions",
                              operational_store=ops, history_selected=True,
                              legacy_history_disabled=True)
    yield SimpleNamespace(ops=ops, sessions=sessions, snapshot=snapshot, path=path,
                          root=tmp_path)
    snapshot.close()
    ops.close()


def test_new_boundary_survives_restart_without_conversation_text(case, monkeypatch):
    from yeoman_gateway.session.operational import OperationalSessions

    policy = PolicyConfig(owners={"whatsapp": ["10001@s.whatsapp.net"]})
    policy_path = case.root / "synthetic-policy.json"
    policy_path.write_text(policy.model_dump_json(by_alias=True))
    adapter = EnginePolicyAdapter(engine=PolicyEngine(policy, workspace=case.root),
                                  known_tools=set(), policy_path=policy_path,
                                  session_manager=case.sessions)
    monkeypatch.setattr(adapter, "_now_ms", lambda: 20)
    ctx = AdminCommandContext("whatsapp", "dm:synthetic", "10001@s.whatsapp.net",
                              None, False, "/new")
    assert adapter.new_session_handle(replace(ctx, sender_id="10002@s.whatsapp.net"), []).status == "ignored"
    assert case.ops.boundary(channel="whatsapp", chat_id=ctx.chat_id) is None
    assert adapter.new_session_handle(ctx, []).outcome == "applied"
    case.ops.close()
    case.ops = OperationalSessions(case.path)
    case.sessions = SessionManager(case.root, sessions_dir=case.root / "sessions",
                                  operational_store=case.ops, history_selected=True,
                                  legacy_history_disabled=True)
    session = case.sessions.get_or_create("opaque:thread:key", channel="whatsapp",
                                         chat_id=ctx.chat_id, thread_id="t:1",
                                         history_snapshot=case.snapshot)
    assert session.get_history() == [{"role": "system", "content":
        "[legacy chat context - not thread-bound]\nuser: current"}]
    assert [row["message_id"] for row in HistoryQueries(case.snapshot).search(
        chat_ids=(ctx.chat_id,), query="older", limit=10)] == ["equal", "old", "undated"]
    assert case.ops.boundary(channel="whatsapp", chat_id=ctx.chat_id) == 20
    assert adapter.new_session_handle(replace(ctx, sender_id="10002@s.whatsapp.net"), []).status == "ignored"
    with sqlite3.connect(case.path) as db:
        assert db.execute("SELECT channel,chat_id FROM boundaries").fetchall() == [("whatsapp", ctx.chat_id)]
        assert db.execute("SELECT channel,chat_id,thread_id FROM routes WHERE session_key='opaque:thread:key'").fetchone() == ("whatsapp", ctx.chat_id, "t:1")
        for table in ("boundaries", "routes"):
            assert not {"content", "text", "arguments", "result"}.intersection(
                row[1] for row in db.execute(f"PRAGMA table_info({table})"))
    case.sessions.save(session)
    assert not list(case.root.rglob("*.jsonl"))


def test_telegram_sessions_continue_and_whatsapp_tool_trace_is_audit_only(case):
    telegram = case.sessions.get_or_create("telegram:42", channel="telegram", chat_id="42")
    telegram.add_message("user", "telegram words")
    case.sessions.save(telegram)
    assert list((case.root / "sessions").glob("*.jsonl"))
    session = case.sessions.get_or_create("opaque", channel="whatsapp", chat_id="dm:synthetic",
                                         history_snapshot=case.snapshot, turn_id="turn-1")
    session.add_message("assistant", "ephemeral")
    session.add_tool_call("synthetic_tool", "call-1", {"x": 1}, "audit result")
    case.sessions.save(session)
    assert all(row.get("role") != "tool_trace" for row in session.messages)
    restarted = case.sessions.get_or_create("opaque", channel="whatsapp", chat_id="dm:synthetic",
                                           history_snapshot=case.snapshot, turn_id="turn-2")
    assert "ephemeral" not in str(restarted.get_history())
    assert "audit result" not in str(HistoryQueries(case.snapshot).recent(chat_id="dm:synthetic", limit=10))
    with sqlite3.connect(case.path) as db:
        row = db.execute("SELECT session_key,turn_id,tool_call_id,tool_name,arguments,result,at_ms FROM tool_traces").fetchone()
        assert row[:4] == ("opaque", "turn-1", "call-1", "synthetic_tool")
        assert json.loads(row[4]) == {"x": 1} and row[5] == "audit result"
    args = dict(session_key=row[0], turn_id=row[1], tool_call_id=row[2], tool_name=row[3],
                arguments=row[4], result=row[5], at_ms=row[6])
    case.ops.record_tool_trace(**args)
    with pytest.raises(ValueError):
        case.ops.record_tool_trace(**{**args, "result": "different"})
    with sqlite3.connect(case.path) as db:
        assert db.execute("SELECT count(*) FROM tool_traces").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM tool_traces")


def test_new_boundary_applies_to_thread_and_implicit_followup(case):
    session = case.sessions.get_or_create("thread:opaque", channel="whatsapp",
                                         chat_id="dm:synthetic", thread_id="t:1",
                                         history_snapshot=case.snapshot)
    case.ops.set_boundary(channel="whatsapp", chat_id="dm:synthetic", at_ms=20)
    assert session.get_history() == [{"role": "system", "content":
        "[legacy chat context - not thread-bound]\nuser: current"}]
    assert [row["content"] for row in case.sessions.recent_history(
        channel="whatsapp", chat_id="dm:synthetic", snapshot=case.snapshot, limit=20)] == ["current"]
    wrapper = ThreadActorResponder(inner=SimpleNamespace(sessions=case.sessions), actors=None, store=None)
    context = wrapper._ensure_legacy_context(channel="whatsapp", chat_id="dm:synthetic",
                                              session_key="thread:opaque", history_snapshot=case.snapshot)
    assert "current" in context and "older" not in context
    assert not list(case.root.rglob("*.jsonl"))
    with pytest.raises(HistoryPaused):
        case.sessions.get_or_create("thread:opaque", channel="whatsapp", chat_id="dm:synthetic",
                                    thread_id="t:1").get_history()


def test_boundary_import_is_explicit_and_preserves_last_marker(case):
    from yeoman_gateway.session.operational import import_session_boundaries

    source = case.root / "frozen"
    source.mkdir()
    (source / "whatsapp_dm.jsonl").write_text(
        json.dumps({"_type": "metadata", "metadata": {}}) + "\n" +
        json.dumps({"role": "session_boundary", "timestamp": "2026-01-01T00:00:00+00:00"}) + "\n")
    assert import_session_boundaries(source, case.ops) == {"whatsapp:dm": 1767225600000}
    assert case.ops.boundary(channel="whatsapp", chat_id="dm") == 1767225600000
    assert import_session_boundaries(source, case.ops) == {"whatsapp:dm": 1767225600000}


def test_retired_session_routing_keeps_legacy_non_whatsapp_callers(case):
    telegram = case.sessions.get_or_create("telegram:42")
    telegram.add_message("user", "telegram legacy caller")
    case.sessions.save(telegram)
    assert case.sessions._load("telegram:42").get_history() == [
        {"role": "user", "content": "telegram legacy caller", "timestamp": telegram.messages[0]["timestamp"]}]
    with pytest.raises(ValueError):
        case.sessions.get_or_create("whatsapp:dm:synthetic:thread:1")


def test_defaults_create_only_legacy_session_files(tmp_path):
    sessions = SessionManager(tmp_path, sessions_dir=tmp_path / "sessions")
    session = sessions.get_or_create("whatsapp:synthetic")
    session.add_message("user", "default legacy text")
    session.add_boundary()
    session.add_message("assistant", "after boundary")
    sessions.save(session)
    assert sessions._load(session.key).get_history()[0]["content"] == "after boundary"
    assert not (tmp_path / "ops").exists()


def test_import_validation_leaves_boundaries_unchanged(case, monkeypatch):
    from yeoman_gateway.session import operational

    source = case.root / "frozen"
    source.mkdir()
    valid = {"role": "session_boundary", "timestamp": "2026-01-01T00:00:00+00:00"}
    (source / "whatsapp_dm.jsonl").write_text(json.dumps(valid) + "\n")
    (source / "whatsapp_other.jsonl").write_text('{"role":"session_boundary"}\n')
    with pytest.raises(KeyError):
        operational.import_session_boundaries(source, case.ops)
    assert case.ops.boundary(channel="whatsapp", chat_id="dm") is None
    monkeypatch.setattr(operational, "get_operational_store_path",
                        lambda *args, **kwargs: source / "ops" / "session-metadata.db")
    with pytest.raises(ValueError, match="isolated"):
        operational.import_session_boundaries(source, case.ops)
    assert case.ops.boundary(channel="whatsapp", chat_id="dm") is None


@pytest.mark.asyncio
async def test_selected_history_excludes_current_inbound_and_has_no_duplicates(case, monkeypatch):
    class Provider(LLMProvider):
        async def chat(self, **kwargs):
            return LLMResponse(content="synthetic answer")

        def get_default_model(self):
            return "synthetic/model"

    db = case.snapshot.connection
    db.execute("UPDATE messages SET direction='out', text='prior reply' WHERE message_id='equal'")
    db.execute("INSERT INTO messages SELECT 'foreign', channel, 'other-chat', native_message_id,"
               "sender_contact_id,sender_identifier,sender_basis,direction,sent_ms,time_certainty,"
               "text,media_json,reply_to_native_id,mentions_json,provenance,source_refs"
               " FROM messages WHERE message_id='new'")
    session = case.sessions.get_or_create("opaque", channel="whatsapp", chat_id="dm:synthetic",
                                         history_snapshot=case.snapshot)
    session.current_message_id = "new"
    session.add_message("user", "current", message_id="new")
    session.add_message("assistant", "prior reply", message_id="equal")
    session.add_message("assistant", "later ephemeral reply", message_id="not-yet-projected")
    session.add_message("assistant", "prior reply", message_id="different-native-id")
    history = session.get_history()
    assert [row.get("message_id") for row in history] == [
        "undated", "old", "equal", "not-yet-projected", "different-native-id"]
    assert "current" not in [row["content"] for row in history]
    assert sum(row["content"] == "prior reply" for row in history) == 2  # Distinct IDs, same text.
    responder = LLMResponder(provider=Provider(), workspace=case.root, bus=MessageBus(),
                             session_manager=case.sessions)
    observed = {}
    original = responder.context.build_messages

    def build_messages(**kwargs):
        observed.update(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(responder.context, "build_messages", build_messages)
    try:
        result = await responder._generate(
            session_key="opaque", channel="whatsapp", chat_id="dm:synthetic", content="current",
            sender_id="10001@s.whatsapp.net", media=(), metadata={"message_id": "new"},
            allowed_tools=set(), persona_text=None, history_snapshot=case.snapshot)
        assert result == "synthetic answer"
        assert observed["current_message"] == "current"
        assert [row["message_id"] for row in observed["history"]] == ["undated", "old", "equal"]
        assert all(row["content"] != "current" for row in observed["history"])
    finally:
        await responder.aclose()


def test_sessions_path_can_resolve_without_creating_directories():
    root = Path(os.environ["YEOMAN_HOME"])
    before = set(root.rglob("*"))
    assert get_sessions_path(create=False) == root / "data" / "inbound"
    assert set(root.rglob("*")) == before
    assert get_sessions_path() == root / "data" / "inbound"
    assert (root / "data" / "inbound").is_dir()
