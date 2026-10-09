"""Real offline compositions for the six selected history readers."""
from __future__ import annotations

import asyncio
import os
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

from yeoman_gateway.history.context import history_knowledge_scope, history_turn
from yeoman_gateway.history.live import HistoryPaused
from yeoman_gateway.knowledge.models import TrustedReadContext

FAMILIES = ("knowledge", "whatsapp", "responder", "tools", "participation", "secondary")
_FIELDS = frozenset((
    "workspace_id", "channel", "chat_id", "principal", "phone", "message_id",
    "source_event_id", "statement_id", "query", "text", "curated_text", "at_ms",
    "owner_scope", "rights",
))
_RIGHTS = frozenset(("knowledge_read", "tools_read", "owner_export"))


def _inputs(record: Mapping[str, Any]) -> tuple[dict[str, Any], Path, Path]:
    try:
        layout = record["layout"]
        diagnostics = record["inventory"]["reader_smoke"]
        knowledge_path = Path(layout["knowledge_live"])
        policy_path = Path(layout["policy_snapshot"])
    except (KeyError, TypeError) as exc:
        raise ValueError("reader_smoke_inputs_missing") from exc
    if not isinstance(diagnostics, Mapping) or set(diagnostics) != _FIELDS:
        raise ValueError("reader_smoke_inputs_missing_or_unknown")
    if not all(isinstance(diagnostics.get(key), str) and diagnostics[key]
               for key in ("workspace_id", "channel", "chat_id", "principal", "phone",
                           "message_id", "source_event_id", "statement_id", "query", "text",
                           "curated_text")):
        raise ValueError("reader_smoke_inputs_invalid")
    if diagnostics["channel"] != "whatsapp" or type(diagnostics["at_ms"]) is not int:
        raise ValueError("reader_smoke_inputs_invalid")
    if type(diagnostics["owner_scope"]) is not bool:
        raise ValueError("reader_smoke_inputs_invalid")
    rights = diagnostics["rights"]
    if not isinstance(rights, Mapping) or set(rights) != _RIGHTS or any(type(v) is not bool for v in rights.values()):
        raise ValueError("reader_smoke_inputs_invalid")
    if not all(rights.values()):
        raise ValueError("reader_smoke_rights_unselected")
    for path in (knowledge_path, policy_path):
        if not path.is_absolute() or any(parent.is_symlink() for parent in (path, *path.parents)):
            raise ValueError("reader_smoke_path_invalid")
    return dict(diagnostics), knowledge_path, policy_path


@contextmanager
def _knowledge(path: Path, policy_path: Path, workspace_id: str):
    from yeoman_gateway.knowledge import open_knowledge_store
    from yeoman_gateway.knowledge.runtime import RuntimeKnowledgePolicy, RuntimeKnowledgeSources
    from yeoman_gateway.policy.loader import load_policy

    if not path.is_file() or not policy_path.is_file():
        raise ValueError("reader_smoke_path_missing")
    policy_config = load_policy(policy_path)
    policy = RuntimeKnowledgePolicy(engine=policy_config)
    knowledge = open_knowledge_store(
        path, workspace_id=workspace_id, source_authority=RuntimeKnowledgeSources(),
        policy_authority=policy, create=False, history_mode=True,
        legacy_history_disabled=True,
    )
    try:
        yield knowledge, policy, policy_config
    finally:
        knowledge.close()


def _offline(snapshot: Any) -> Any:
    async def borrow():
        return snapshot
    return SimpleNamespace(read_turn=borrow, health=lambda: {
        "status": "ready", "generation": snapshot.generation,
    })


def _context(knowledge: Any, d: Mapping[str, Any]) -> TrustedReadContext:
    return TrustedReadContext(
        d["principal"], d["channel"], d["chat_id"], frozenset({d["principal"]}),
        None, knowledge.policy_revision, "reply", d["at_ms"],
        is_direct=not d["chat_id"].endswith("@g.us"),
    )


def _source_for(knowledge: Any, snapshot: Any, d: Mapping[str, Any]):
    from yeoman_gateway.history.queries import HistoryQueries

    native = HistoryQueries(snapshot).native_message(
        chat_id=d["chat_id"], native_id=d["source_event_id"],
    )
    if native is None or native["message_id"] != d["message_id"]:
        raise ValueError("reader_smoke_native_source_mismatch")
    rows = knowledge._store.query(
        "SELECT event_id, revision FROM knowledge_history_source_aliases WHERE message_id=?",
        (d["message_id"],),
    )
    matches = [knowledge.history_source_ledger.alias((row["event_id"], row["revision"]))
               for row in rows]
    matches = [alias for alias in matches if alias is not None]
    if len(matches) != 1 or matches[0].message_id != d["message_id"]:
        raise ValueError("reader_smoke_source_alias_ambiguous")
    alias = matches[0]
    if not knowledge._authority.verify_source(alias.issued):
        raise ValueError("reader_smoke_source_unverified")
    return alias.issued


def _required_selection_refusal():
    from yeoman_gateway.history.export import secondary_archive
    blocked = SimpleNamespace(
        live_projection_enabled=True, legacy_writers_disabled=True,
        readers=SimpleNamespace(secondary=False),
    )
    try:
        secondary_archive(None, blocked)
    except HistoryPaused:
        return True
    return False


async def _compose(family: str, snapshot: Any, d: Mapping[str, Any], home: Path,
                   knowledge: Any, policy: Any, policy_config: Any) -> dict[str, bool]:
    from yeoman_gateway.adapters.reply_archive_history import HistoryReplyArchiveAdapter
    from yeoman_gateway.history.export import read_history_turn
    from yeoman_gateway.history.queries import HistoryQueries

    q = HistoryQueries(snapshot)
    archive = HistoryReplyArchiveAdapter(q)
    context = _context(knowledge, d)
    result: dict[str, bool] = {"unselected_refused": _required_selection_refusal()}
    if not result["unselected_refused"]:
        return result

    if family == "knowledge":
        _source_for(knowledge, snapshot, d)
        recall = knowledge.recall(
            __import__("yeoman_gateway.knowledge.models", fromlist=["RecallQuery"]).RecallQuery(d["query"]),
            context=context,
        )
        if recall.statement_ids != (d["statement_id"],) or d["curated_text"] not in recall.text:
            raise ValueError("reader_smoke_curated_disclosure_failed")
        result.update(mapped_source=True, curated_disclosure=True)
    elif family == "whatsapp":
        message = archive.lookup_message(d["channel"], d["chat_id"], d["source_event_id"])
        if message is None or q.mention(d["phone"], chat_id=d["chat_id"], at_ms=d["at_ms"]) != d["phone"]:
            raise ValueError("reader_smoke_reply_identity_failed")
        result.update(reply_identity=True, canonical_mentions=True)
    elif family == "responder":
        from yeoman_gateway.session.manager import SessionManager
        from yeoman_gateway.session.operational import OperationalSessions
        operational = OperationalSessions(home / "reader-operational.db")
        try:
            sessions = SessionManager(
                home, sessions_dir=home / "reader-frozen-sessions", history_selected=True,
                legacy_history_disabled=True, operational_store=operational,
            )
            recent = sessions.recent_history(
                channel=d["channel"], chat_id=d["chat_id"], snapshot=snapshot, limit=20,
            )
            if not recent:
                raise ValueError("reader_smoke_recent_window_empty")
            session = sessions.get_or_create(
                f"{d['channel']}:{d['chat_id']}", channel=d["channel"], chat_id=d["chat_id"],
                history_snapshot=snapshot,
            )
            session.add_boundary()
            if session.get_history() or (home / "reader-frozen-sessions").exists():
                raise ValueError("reader_smoke_new_boundary_failed")
        finally:
            operational.close()
        result.update(recent_window=True, operational_new=True)
    elif family == "tools":
        from yeoman_gateway.agent.tools.recall_conversation import RecallConversationTool
        from yeoman_gateway.knowledge.models import KnowledgeError
        from yeoman_gateway.processing.tool_context import (
            ToolInvocationContext,
            reset_tool_context,
            set_tool_context,
        )
        tool = RecallConversationTool(None)
        tool._history_selected = True
        token = set_tool_context(ToolInvocationContext(
            channel=d["channel"], chat_id=d["chat_id"], history_snapshot=snapshot,
        ))
        try:
            found = await tool.execute(query=d["text"])
            if "Found" not in found or not q.media(chat_id=d["chat_id"], limit=10):
                raise ValueError("reader_smoke_tool_search_failed")
            try:
                read_history_turn(snapshot, context=context, chat_ids=("unauthorized@g.us",),
                                  after_ms=0, limit=10)
            except KnowledgeError:
                result.update(fts=True, media=True, unauthorized_denial=True)
            else:
                raise ValueError("reader_smoke_unauthorized_read_allowed")
        finally:
            reset_tool_context(token)
    elif family == "participation":
        from yeoman_gateway.policy.engine import ActorContext, PolicyEngine
        from yeoman_gateway.processing.participation import ParticipationOpportunity
        from yeoman_gateway.processing.participation_context import (
            ParticipationContextBounds,
            ParticipationContextBuilder,
            ParticipationDecisionInputs,
        )
        source = q.native_message(chat_id=d["chat_id"], native_id=d["source_event_id"])
        if source is None:
            raise ValueError("reader_smoke_participation_source_missing")
        participation_policy = PolicyEngine(policy_config, home)
        expected_row = {
            **archive.row(d["channel"], d["chat_id"], d["source_event_id"]),
            "message_id": d["source_event_id"],
        }

        def source_authorized(row: Mapping[str, Any]) -> bool:
            sender = str(row.get("sender_id") or row.get("participant") or "")
            if not sender or row != expected_row:
                return False
            actor = ActorContext(
                d["channel"], d["chat_id"], sender, [sender],
                d["chat_id"].endswith("@g.us"), False, False,
            )
            return participation_policy.evaluate(actor, set()).accept_message

        builder = ParticipationContextBuilder(
            archive=None, policy=participation_policy, source_authorizer=source_authorized,
        )
        builder._history_selected = True
        opportunity = ParticipationOpportunity(
            opportunity_id="history-cutover-reader-smoke", channel=d["channel"], chat_id=d["chat_id"],
            trigger="inbound", source_event_ids=(d["source_event_id"],), observed_revision=1,
            activation_epoch=1, created_at_ms=d["at_ms"],
        )
        inputs = ParticipationDecisionInputs(
            snapshot={"guidance": "synthetic bounded probe", "allowed_contribution_types": ("observation",)},
            bounds=ParticipationContextBounds(), allowed_actions=("silence", "comment"),
            allowed_intents=frozenset(("initiate",)), remaining_budgets=(),
            reservation_limits_by_intent=(("initiate", (("comment", 1, 60_000),)),),
            approval_required=False, arbitration_revision=1,
            current_source_ids=(d["source_event_id"],), continuation_candidate=False,
        )
        value = await builder.build(opportunity, inputs=inputs, now_ms=d["at_ms"])
        if not value or q.audience(d["message_id"]).status != "known":
            raise ValueError("reader_smoke_participation_failed")
        result.update(ambient=True, audience_generation=True)
    else:
        owner_context = replace(context, owner=True)
        value = read_history_turn(snapshot, context=owner_context, chat_ids=(d["chat_id"],),
                                  after_ms=0, limit=10)
        if value["count"] > 10 or "messages" in value:
            raise ValueError("reader_smoke_owner_export_unbounded")
        try:
            from yeoman_gateway.history.export import read_history_export
            await read_history_export(_offline(snapshot), context=context, chat_ids=(d["chat_id"],),
                                      after_ms=0, limit=10)
        except HistoryPaused:
            pass
        else:
            raise ValueError("reader_smoke_in_turn_export_allowed")
        prior = os.environ.get("YEOMAN_HISTORY_TOOL_TURN")
        os.environ["YEOMAN_HISTORY_TOOL_TURN"] = "1"
        try:
            from yeoman_gateway.history.export import request_history_read
            try:
                await request_history_read(home / "missing-history.sock", {})
            except HistoryPaused:
                pass
            else:
                raise ValueError("reader_smoke_subprocess_read_allowed")
        finally:
            if prior is None:
                os.environ.pop("YEOMAN_HISTORY_TOOL_TURN", None)
            else:
                os.environ["YEOMAN_HISTORY_TOOL_TURN"] = prior
        result.update(bounded_owner_export=True, in_turn_subprocess_refusal=True)
    return result


def build_probes(*, record: Mapping[str, Any], home: Path) -> dict[str, Any]:
    """Build lazily opened, source-backed adapter probes for one approved record."""
    diagnostics, knowledge_path, policy_path = _inputs(record)
    home = Path(home)
    if not home.is_absolute() or any(parent.is_symlink() for parent in (home, *home.parents)):
        raise ValueError("reader_smoke_home_invalid")

    def callback(family: str):
        def probe(snapshot: Any) -> Mapping[str, bool]:
            home.mkdir(parents=True, exist_ok=True, mode=0o700)
            with _knowledge(knowledge_path, policy_path, diagnostics["workspace_id"]) as (
                knowledge, policy, policy_config,
            ):
                if diagnostics["owner_scope"] != (policy.admin_actor() == diagnostics["principal"]):
                    raise ValueError("reader_smoke_owner_scope_mismatch")
                async def composition():
                    async with history_turn(_offline(snapshot)):
                        with history_knowledge_scope(snapshot, knowledge):
                            _source_for(knowledge, snapshot, diagnostics)
                            return await _compose(
                                family, snapshot, diagnostics, home, knowledge, policy, policy_config,
                            )
                return asyncio.run(composition())
        return probe

    return {family: callback(family) for family in FAMILIES}
