"""Test support for the statement-capture suite.

Synthetic and offline: temporary databases, a fake chat registry and an injected
extractor.  The harness wires the *production* components - the Bridge signal sink, the
observation registrar, the promotion producer, the capture worker and the ordinary memory
read path - so a test proves the real path rather than a re-implementation of it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from yeoman_gateway.knowledge import open_knowledge_store, workspace_id_for
from yeoman_gateway.knowledge._capture import (
    ObservedSourceRegistrar,
    StatementCaptureProducer,
)
from yeoman_gateway.knowledge._capture_worker import (
    STATEMENT_EXTRACTOR_VERSION,
    StatementCaptureWorker,
    StatementDraft,
)
from yeoman_gateway.knowledge._memory import MemoryService
from yeoman_gateway.knowledge.runtime import RuntimeKnowledgePolicy, RuntimeKnowledgeSources
from yeoman_gateway.processing.signals import SignalJournalSink
from yeoman_gateway.processing.store import ProcessingStore
from yeoman_shared.config.schema import MemoryConfig

GROUP = "491786127564-1611913127@g.us"
DIRECT = "491757070305@s.whatsapp.net"
AUTHOR = "491757070305"
OTHER = "491511"

START_MS = 1_700_000_000_000


class Registry:
    """Stand-in for the live chat registry: proven participants per chat."""

    def __init__(self, chats: dict[tuple[str, str], list[str]] | None = None) -> None:
        self.chats = chats if chats is not None else {("whatsapp", GROUP): [AUTHOR, OTHER]}

    def get_chat(self, channel: str, chat_id: str) -> Any:
        members = self.chats.get((channel, chat_id))
        if members is None:
            return None
        return {"metadata": {"participants": [f"{item}@s.whatsapp.net" for item in members]}}


class CaptureHarness:
    """One synthetic runtime with the real capture path wired together."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        registry: Registry | None = None,
        idle_ms: int = 60_000,
        max_delay_ms: int = 300_000,
        batch_max: int = 8,
        max_waiting: int = 64,
    ) -> None:
        self.tmp_path = tmp_path
        self.now = START_MS
        self.store = ProcessingStore(tmp_path / "processing.db")
        self.registry = registry if registry is not None else Registry()
        self.authority = RuntimeKnowledgeSources(processing_store=self.store)
        self.policy = RuntimeKnowledgePolicy(
            engine=None, chat_registry=self.registry, policy_revision=1
        )
        self.knowledge = open_knowledge_store(
            tmp_path / "knowledge.db",
            workspace_id=workspace_id_for(tmp_path),
            source_authority=self.authority,
            policy_authority=self.policy,
            clock=self,
        )
        self.memory = MemoryService(
            workspace=tmp_path,
            config=MemoryConfig.model_validate(
                {
                    "dbPath": str(tmp_path / "memory.db"),
                    # The ordinary responder read path only injects shared facts when the
                    # shared-fact lane is on; the statement nodes live in that lane.
                    "shared": {"enabled": True, "extractionEnabled": False},
                }
            ),
            store=self.knowledge.memory_store(),
            owns_store=False,
        )
        self.registrar = ObservedSourceRegistrar(
            knowledge=self.knowledge,
            processing=self.store,
            chat_registry=self.registry,
            clock=self,
        )
        self.sink = SignalJournalSink(
            self.store,
            clock=self,
            sources=self.registrar,
            statements=self.knowledge,
        )
        self.producer = StatementCaptureProducer(
            knowledge=self.knowledge,
            processing=self.store,
            idle_ms=idle_ms,
            max_delay_ms=max_delay_ms,
            batch_max=batch_max,
            max_waiting=max_waiting,
            extractor_version=STATEMENT_EXTRACTOR_VERSION,
            clock=self,
        )
        self.drafts: list[StatementDraft] = []
        self.extractor_calls: list[tuple[str, ...]] = []
        self.worker: StatementCaptureWorker | None = None
        self._counter = 0

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
        if self.worker is not None:
            self.worker.stop()
        self.memory.close()
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
        stored = self.sink.capture(
            "message",
            payload,
            event_id=event_id,
            event_key=f"whatsapp:{account}:{chat_id}:message:{provider_id}",
            account=account,
            observed_at_ms=self.now,
            strict=True,
        )
        assert stored == event_id
        return str(stored)

    def append_raw(
        self,
        *,
        text: str,
        message_id: str,
        chat_id: str = GROUP,
        principal: str = AUTHOR,
        kind: str = "message",
        direction: str = "in",
        extra: dict[str, Any] | None = None,
    ) -> str:
        """Journal one event directly, for shapes the Bridge sink does not produce.

        Used for assistant output and other already-enriched payloads; it still lands in
        the canonical journal, which is the point: observation is not the producer's job.
        """
        event_id = f"raw_{message_id}"
        payload: dict[str, Any] = {
            "kind": kind,
            "origin": "whatsapp_bridge",
            "channel": "whatsapp",
            "chat_id": chat_id,
            "text": text,
        }
        payload.update(extra or {})
        stored = self.store.append_event(
            event_key=f"raw:{chat_id}:{kind}:{message_id}",
            event_id=event_id,
            trace_id=f"raw:{chat_id}:{message_id}",
            payload=payload,
            now_ms=self.now,
            direction=direction,
        )
        assert stored == event_id
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
        assert revoked == event_id
        return (provider_message_id,)

    def source_row(self, event_id: str, revision: int = 1) -> dict[str, Any]:
        entry = self.store.get_event_source_authority(event_id, revision)
        assert entry is not None
        return entry

    # ── promotion ────────────────────────────────────────────────────────────

    def activate(self) -> None:
        """Set the forward boundary: only observations after it are promoted."""
        self.producer.initialize_boundary(now_ms=self.now)

    def extractor(self, items: Sequence[Any]) -> list[StatementDraft]:
        self.extractor_calls.append(tuple(item.text for item in items))
        return list(self.drafts)

    def build_worker(self, **overrides: Any) -> StatementCaptureWorker:
        options: dict[str, Any] = {
            "knowledge": self.knowledge,
            "processing": self.store,
            "extractor": self.extractor,
            "producer": self.producer,
            "clock": self,
        }
        options.update(overrides)
        self.worker = StatementCaptureWorker(**options)
        return self.worker

    def promote(self) -> Any:
        """Run one promotion pass (no extraction)."""
        return self.producer.run_due(now_ms=self.now)

    def run_capture(self, **overrides: Any) -> Any:
        """Run one worker pass: promote, then extract and publish."""
        worker = self.worker or self.build_worker(**overrides)
        return worker.run_due(now_ms=self.now)

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

    def recall(
        self, query: str, *, principal: str = f"{AUTHOR}@s.whatsapp.net", chat_id: str = GROUP
    ) -> str:
        """The ordinary responder recall: shared-fact retrieval for a verified reader."""
        from yeoman_gateway.knowledge._memory.read_gate import build_read_context

        context = build_read_context(
            principal_id=principal,
            channel="whatsapp",
            chat_id=chat_id,
            chat_registry=self.registry,
            policy=None,
            now_ms=self.now,
            epoch=self.memory.store.acl_epoch(),
            is_direct=not chat_id.endswith("@g.us"),
            counterpart=chat_id if not chat_id.endswith("@g.us") else None,
        )
        result = self.memory.retrieve_for_context(query=query, read_context=context)
        return str(result.text or "")

    def knowledge_recall(
        self, query: str, *, principal: str = f"{AUTHOR}@s.whatsapp.net", chat_id: str = GROUP
    ) -> Any:
        """The statement layer's own gated recall, for scope and identity assertions."""
        from yeoman_gateway.knowledge.models import RecallQuery, TrustedReadContext

        members = frozenset(
            f"{item}@s.whatsapp.net"
            for item in (self.registry.get_chat("whatsapp", chat_id) or {})
            .get("metadata", {})
            .get("participants", [])
        )
        context = TrustedReadContext(
            principal_id=principal,
            channel="whatsapp",
            chat_id=chat_id,
            recipient_principals=members,
            membership_revision=f"mem-{chat_id}-{len(members)}",
            policy_revision=1,
            purpose="reply",
            now_ms=self.now,
            is_direct=not chat_id.endswith("@g.us"),
        )
        return self.knowledge.recall(RecallQuery(text=query, limit=10), context=context)
