"""Test support for the statement-capture suite.

Synthetic and offline: temporary databases, projected native History attestations,
and an injected extractor. The harness wires the *production* components - the Bridge signal sink, the
observation registrar, the promotion producer, the capture worker and the ordinary memory
read path - so a test proves the real path rather than a re-implementation of it.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Sequence

from yeoman_gateway.history.live import HistoryBoundary
from yeoman_gateway.history.reader import HistoryReader
from yeoman_gateway.knowledge import open_knowledge_store, workspace_id_for
from yeoman_gateway.knowledge._capture import (
    ObservedSourceRegistrar,
)
from yeoman_gateway.knowledge._capture_worker import (
    StatementDraft,
)
from yeoman_gateway.knowledge._history_capture import HistoryCaptureProducer, HistoryCaptureWorker
from yeoman_gateway.knowledge.runtime import RuntimeKnowledgePolicy, RuntimeKnowledgeSources
from yeoman_gateway.policy.engine import PolicyEngine
from yeoman_gateway.policy.schema import PolicyConfig
from yeoman_gateway.processing.signals import SignalJournalSink
from yeoman_gateway.processing.store import ProcessingStore
from yeoman_shared.raw_archive.records import SourceBoundary

from tests.gateway.history_test_helpers import (
    ProjectedHistory,
    prepare_empty_history_knowledge,
)

GROUP = "491786127564-1611913127@g.us"
DIRECT = "491757070305@s.whatsapp.net"
AUTHOR = "491757070305"
OTHER = "491511"

START_MS = 1_700_000_000_000


class CaptureHarness:
    """One synthetic runtime with the real capture path wired together."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        history_members: Sequence[str] = (AUTHOR, OTHER),
        idle_ms: int = 60_000,
        max_delay_ms: int = 300_000,
        batch_max: int = 8,
        max_waiting: int = 64,
    ) -> None:
        self.tmp_path = tmp_path
        self.now = START_MS
        self.store = ProcessingStore(tmp_path / "processing.db")
        self.history_members = tuple(history_members)
        self.history = ProjectedHistory(tmp_path / "projected-history")
        self.history.health = lambda: {
            "status": "ready", "reason": None, "generation": self.history.generation
        }

        async def worker_snapshot(callback: Any) -> Any:
            with self._snapshot() as snapshot:
                return callback(snapshot)

        self.history.worker_snapshot = worker_snapshot
        self.authority = RuntimeKnowledgeSources(processing_store=self.store)
        self.policy = RuntimeKnowledgePolicy(engine=None, policy_revision=1)
        self.knowledge = open_knowledge_store(
            prepare_empty_history_knowledge(tmp_path, "knowledge"),
            workspace_id=workspace_id_for(tmp_path),
            source_authority=self.authority,
            policy_authority=self.policy,
            clock=self,

        )
        self.registrar = ObservedSourceRegistrar(
            knowledge=self.knowledge,
            processing=self.store,
            clock=self,
        )
        self.sink = SignalJournalSink(
            self.store,
            clock=self,
            sources=self.registrar,
            statements=self.knowledge,
        )
        self.sink._history_projector = self.history
        self.sink._history_knowledge = self.knowledge
        policy_engine = PolicyEngine(PolicyConfig.model_validate({
            "defaults": {"whoCanTalk": {"mode": "everyone"}},
            "channels": {"whatsapp": {"chats": {GROUP: {}}}},
        }), tmp_path)
        self.policy.engine = policy_engine
        self.producer = HistoryCaptureProducer(
            knowledge=self.knowledge,
            idle_ms=idle_ms,
            max_delay_ms=max_delay_ms,
            batch_max=batch_max,
            max_waiting=max_waiting,
        )
        self.drafts: list[StatementDraft] = []
        self.extractor_calls: list[tuple[str, ...]] = []
        self.worker: HistoryCaptureWorker | None = None
        self._counter = 0
        self._journal_messages: dict[str, str] = {}
        self._journal_chats: dict[str, str] = {}
        self._activated = False

    # ── clock ────────────────────────────────────────────────────────────────

    def __call__(self) -> int:
        return self.now

    def now_ms(self) -> int:
        return self.now

    def advance(self, delta_ms: int) -> int:
        self.now += int(delta_ms)
        return self.now

    # ── lifecycle ────────────────────────────────────────────────────────────

    def close(self) -> None:
        self.history.close()
        self.knowledge.close()
        self.store.close()

    # ── observation ──────────────────────────────────────────────────────────

    def observe(
        self,
        text: str,
        *,
        chat_id: str = GROUP,
        sender: str = AUTHOR,
        message_id: str | None = None,
        account: str = "default",
        role: str | None = None,
        occurred_ms: int | None = None,
    ) -> str:
        """Journal one provider message through the canonical Bridge sink."""
        self._counter += 1
        provider_id = message_id or f"3EB0{self._counter:04d}"
        payload: dict[str, Any] = {
            "chatJid": chat_id,
            "messageId": provider_id,
            "senderId": sender,
            "text": text,
            "isGroup": chat_id.endswith("@g.us"),
            "timestamp": int(occurred_ms if occurred_ms is not None else self.now),
        }
        if role is not None:
            payload["role"] = role
        event_id = f"wa_{provider_id}"
        self._project_observation(
            chat_id=chat_id, sender=sender, message_id=provider_id, text=text,
            occurred_ms=int(payload["timestamp"]),
        )
        with self._history_scope():
            stored = self.sink.capture(
                "message",
                payload,
                event_id=event_id,
                event_key=f"whatsapp:{account}:{chat_id}:message:{provider_id}",
                account=account,
                observed_at_ms=self.now,
                strict=True,
            )
        asyncio.run(self.sink.register_observed_source_live(str(stored)))
        assert stored == event_id
        self._journal_messages[event_id] = provider_id
        self._journal_chats[event_id] = chat_id
        return str(stored)

    def project_group_snapshot(self, chat_id: str, members: Sequence[str], *, occurred_ms: int) -> None:
        """Append a native membership snapshot; never mutate a synthetic registry."""
        participants = [f"{member}@s.whatsapp.net" for member in members]
        if not participants:
            return
        for member in members:
            jid = f"{member}@s.whatsapp.net"
            self.history.add_contact(
                identifiers=[jid], name=f"Synthetic {member}", timestamp=occurred_ms
            )
        self.history.add_group(
            chat_id=chat_id, participants=participants, timestamp=occurred_ms
        )

    def _project_observation(
        self, *, chat_id: str, sender: str, message_id: str, text: str,
        occurred_ms: int, direction: str = "in",
    ) -> None:
        """Attest native identity, roster, and message at the message's source time."""
        phone = f"{sender}@s.whatsapp.net"
        contacts = set(self.history_members) | {sender}
        for member in contacts:
            jid = f"{member}@s.whatsapp.net"
            self.history.add_contact(
                identifiers=[jid], name=f"Synthetic {member}", timestamp=occurred_ms
            )
        if chat_id.endswith("@g.us") and self.history_members:
            self.project_group_snapshot(chat_id, self.history_members, occurred_ms=occurred_ms)
        self.history.add_message(
            chat_id=chat_id, message_id=message_id, text=text,
            timestamp=occurred_ms, sender_id=phone, direction=direction,
        )

    @contextmanager
    def _history_scope(self):
        from yeoman_gateway.history.context import history_knowledge_scope

        with self._snapshot() as snapshot:
            with history_knowledge_scope(snapshot, self.knowledge):
                yield snapshot

    @contextmanager
    def _snapshot(self):
        # ProjectedHistory's convenience reader omits source vectors. Capture requires
        # the actual raw prefix so its activation boundary excludes preexisting rows.
        projected = self.history.snapshot()
        projected.close()
        boundaries = []
        if self.history.records:
            count = len(self.history.records)
            boundaries.append(SourceBoundary("whatsapp/events.jsonl", count, count * 100, "synthetic"))
        if self.history.attestations:
            count = len(self.history.attestations)
            boundaries.append(SourceBoundary("owner/attestations.jsonl", count, count * 100, "synthetic"))
        reader = HistoryReader(self.history.path)
        snapshot = reader.open_snapshot(HistoryBoundary(self.history.generation, tuple(boundaries)))
        try:
            yield snapshot
        finally:
            snapshot.close()
            reader.close()

    def append_raw(
        self,
        *,
        text: str,
        message_id: str,
        chat_id: str = GROUP,
        principal: str = AUTHOR,
        kind: str = "message",
        direction: str = "in",
        account: str = "default",
        extra: dict[str, Any] | None = None,
        occurred_ms: int | None = None,
    ) -> str:
        """Journal one event directly, for shapes the Bridge sink does not produce.

        Used for assistant output and other already-enriched payloads; it still lands in
        the canonical journal, which is the point: observation is not the producer's job.
        """
        event_id = f"raw_{message_id}"
        source_time = int(occurred_ms if occurred_ms is not None else self.now)
        payload: dict[str, Any] = {
            "kind": kind,
            "origin": "whatsapp_bridge",
            "channel": "whatsapp",
            "chat_id": chat_id,
            "principal": principal,
            "text": text,
        }
        if kind == "message":
            # A provider identity, so a delete signal can find the row again.
            payload["source_message_id"] = f"raw-provider-{message_id}"
        payload.update(extra or {})
        payload["occurred_ms"] = source_time
        if kind == "message":
            self._project_observation(
                chat_id=chat_id, sender=principal, message_id=f"raw-provider-{message_id}",
                text=text, occurred_ms=source_time, direction=direction,
            )
        stored = self.store.append_event(
            event_key=f"raw:{chat_id}:{kind}:{message_id}",
            event_id=event_id,
            trace_id=f"raw:{chat_id}:{message_id}",
            payload=payload,
            now_ms=source_time,
            account=account,
            direction=direction,
        )
        assert stored == event_id
        if kind == "message":
            self._journal_messages[event_id] = f"raw-provider-{message_id}"
            self._journal_chats[event_id] = chat_id
        return event_id

    def delete(
        self, provider_message_id: str, *, chat_id: str = GROUP, sender: str = AUTHOR
    ) -> tuple[str, ...]:
        """Journal one provider delete through the same canonical path."""
        event_id = f"wa_del_{provider_message_id}"
        revoked = self.sink.capture(
            "delete",
            {
                "chatJid": chat_id,
                "messageId": provider_message_id,
                "senderId": sender,
                "timestamp": self.now,
            },
            event_id=event_id,
            event_key=f"whatsapp:default:{chat_id}:delete:{provider_message_id}",
            account="default",
            observed_at_ms=self.now,
            strict=True,
        )
        self.history.append(
            "delete", chat_id=chat_id, timestamp=self.now,
            messageId=provider_message_id, senderId=f"{sender}@s.whatsapp.net",
        )
        assert revoked == event_id
        return (provider_message_id,)

    def source_row(self, event_id: str, revision: int = 1) -> dict[str, Any]:
        native_id = self._journal_messages[event_id]
        chat_id = self._journal_chats[event_id]
        from yeoman_gateway.history.queries import HistoryQueries
        from yeoman_gateway.knowledge._history_sources import HistoryKnowledgeSources

        with self._snapshot() as snapshot:
            queries = HistoryQueries(snapshot)
            native = queries.native_message(chat_id=chat_id, native_id=native_id)
            assert native is not None
            message_id = native["message_id"]
            row = queries.message(message_id)
            assert row is not None
            audience = queries.audience(message_id)
            source = None
            if audience.status != "unknown":
                sources = HistoryKnowledgeSources(
                    queries, {}, self.knowledge.history_source_ledger,
                    self.knowledge._legacy_authority,
                )
                source = sources.issue(message_id)
                audience = sources.evidence_audience(source, basis="observed_source_batch")
            assert audience is not None
            return {
                "event_id": source.event_id if source is not None else message_id,
                "revision": source.revision if source is not None else revision,
                "author_principal": source.author_principal if source is not None else "",
                "occurred_at_ms": source.occurred_at_ms if source is not None else int(row["sent_ms"]),
                "audience_status": audience.status,
                "audience_members": tuple(sorted(audience.members)),
            }

    # ── promotion ────────────────────────────────────────────────────────────

    def activate(self) -> None:
        """Commit a native-history handover, excluding earlier history from forward capture."""
        if self._activated:
            return
        with self._snapshot() as snapshot:
            # The shared v3 fixture installs a sentinel handover for unrelated tests;
            # this harness replaces it with the real vector-bound native receipt.
            self.knowledge._store.execute(
                "DELETE FROM knowledge_history_capture_state WHERE key='handover'"
            )
            cursor = snapshot.connection.execute(
                "SELECT * FROM messages_current WHERE channel='whatsapp'"
            )
            columns = [column[0] for column in cursor.description]
            rows = [dict(zip(columns, row, strict=True)) for row in cursor]
            classifications = {
                row["message_id"]: self.producer._permanent_reason(row) or "historical_not_selected"
                for row in rows
            }
            self.producer.prepare_handover(
                snapshot, pending=(), processed=(), legacy_boundary=(0, ""),
                classifications=classifications,
            )
        self._activated = True

    def extractor(self, items: Sequence[Any]) -> list[StatementDraft]:
        self.extractor_calls.append(tuple(item.text for item in items))
        return list(self.drafts)

    def build_worker(self, **overrides: Any) -> HistoryCaptureWorker:
        options: dict[str, Any] = {
            "projector": self.history,
            "knowledge": self.knowledge,
            "extractor": self.extractor,
            "producer": self.producer,
        }
        options.update(overrides)
        self.worker = HistoryCaptureWorker(**options)
        return self.worker

    def promote(self) -> Any:
        """Run native-history discovery and enqueue any quiescent sources."""
        self.activate()
        with self._snapshot() as snapshot:
            return self.producer.run_due(snapshot, now_ms=self.now)

    def run_capture(self, **overrides: Any) -> Any:
        """Run one worker pass: promote, then extract and publish."""
        self.activate()
        worker = self.worker or self.build_worker(**overrides)
        return asyncio.run(worker.run_due(now_ms=self.now))

    # ── reads ────────────────────────────────────────────────────────────────

    def statements(self) -> list[dict[str, Any]]:
        rows = self.knowledge._store.query(  # noqa: SLF001 - test assertion on stored rows
            "SELECT s.statement_id, s.status, s.visibility_scope, s.scope_key,"
            " n.content, n.kind FROM knowledge_statements s"
            " JOIN memory2_nodes n ON n.id = s.statement_id"
            " ORDER BY s.created_ms, s.statement_id"
        )
        return [dict(row) for row in rows]

    def statement_texts(self) -> list[str]:
        return [str(row["content"]) for row in self.statements()]

    def jobs(self) -> list[dict[str, Any]]:
        rows = self.knowledge._store.query(  # noqa: SLF001 - test assertion on stored rows
            "SELECT job_id, state, reason, attempts, scope_key, sources_json"
            " FROM knowledge_jobs ORDER BY created_ms, job_id"
        )
        return [dict(row) for row in rows]

    def _audience(self, chat_id: str, *, at_ms: int | None = None):
        from yeoman_gateway.knowledge._history_identity import current_history_scope

        scope = current_history_scope(self.knowledge._store)
        if scope is None:
            raise RuntimeError("native audience lookup requires projected history scope")
        return scope.queries.members(
            chat_id=chat_id, at_ms=int(at_ms if at_ms is not None else self.now)
        )

    def recall(
        self, query: str, *, principal: str = f"whatsapp:{AUTHOR}", chat_id: str = GROUP
    ) -> str:
        """Render recall from the canonical Knowledge store under native history scope."""
        result = self.knowledge_recall(query, principal=principal, chat_id=chat_id)
        return str(getattr(result, "text", "") or "")

    def knowledge_recall(
        self, query: str, *, principal: str = f"whatsapp:{AUTHOR}", chat_id: str = GROUP
    ) -> Any:
        """The statement layer's gated recall with audience derived from projected history."""
        from yeoman_gateway.knowledge.models import RecallQuery, TrustedReadContext

        with self._history_scope():
            audience = self._audience(chat_id)
            members = frozenset(audience.members) if audience.status == "known" else frozenset()
            context = TrustedReadContext(
                principal_id=principal,
                channel="whatsapp",
                chat_id=chat_id,
                recipient_principals=members,
                membership_revision=f"history:{self.history.generation}:{audience.snapshot_id}" if audience.status == "known" else None,
                policy_revision=1,
                purpose="reply",
                now_ms=self.now,
                is_direct=not chat_id.endswith("@g.us"),
            )
            return self.knowledge.recall(RecallQuery(text=query, limit=10), context=context)
