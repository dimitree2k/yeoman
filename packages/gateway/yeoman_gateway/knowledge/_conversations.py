"""Source-linked conversation threads over the canonical event log (Phase 2 / Task 1).

A conversation thread answers "what belongs together by subject", never "which running
Arvid turn receives this input".  Three tables carry it:

* ``conversations`` — one thread, always bound to exactly one chat scope;
* ``conversation_memberships`` — keyed by
  ``(conversation_id, source_event_id, source_revision)``, so one source revision may
  belong to several threads and its text is never copied;
* ``conversation_relations`` — the closed vocabulary ``branches_from``, ``merged_into``,
  ``related_to``.

Rules kept from Phase 1:

* ``ProcessingStore.events`` stays the only source truth; this module stores references,
  never text.
* A source is attached only if the source authority proves it, the capture context
  authorizes it, and it is not revoked.  Otherwise nothing is written at all.
* The chat scope of a membership must match the chat scope of its conversation, so a
  thread can never become a bridge that grants access to another chat.
* Reads run through the same read gate as statements: current membership, current
  recipients, current source audience and current revocation are re-checked before any
  membership row is rendered.
* Split and merge only append.  Old conversation ids and their memberships stay
  readable; a merge writes a redirect instead of rewriting history.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from yeoman_gateway.knowledge.models import (
    CONVERSATION_ORIGINS,
    CONVERSATION_RELATIONS,
    ConversationMembership,
    ConversationMembershipReceipt,
    ConversationMergeReceipt,
    ConversationReferenceReceipt,
    ConversationRelation,
    ConversationSplitReceipt,
    ConversationView,
    KnowledgeError,
    SourceRef,
    TrustedCaptureContext,
    TrustedReadContext,
    ValidationError,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from yeoman_gateway.knowledge._retrieval import RetrievalEngine
    from yeoman_gateway.knowledge._store import KnowledgeStore

#: A reply/quote is recorded as this relation: a candidate link, not a topic merge.
REFERENCE_RELATION = "related_to"

#: Origin recorded for a membership created by an explicit reply/quote reference.
REFERENCE_ORIGINS = {"reply": "explicit_reply", "quote": "explicit_quote"}


def scope_key_of(channel: str, chat_id: str) -> str:
    """The one scope-key convention, shared with the statement and memory stores."""
    return f"channel:{channel}:chat:{chat_id}"


def source_scope_key(source: SourceRef) -> str:
    return scope_key_of(source.channel, source.chat_id)


def _choice(value: object, allowed: tuple[str, ...], field_name: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValidationError(f"{field_name} must be one of {allowed}")
    return value


def _confidence(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError("confidence must be a number")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ValidationError("confidence must be finite and within [0, 1]")
    return number


def _offsets(value: object) -> tuple[int, int] | None:
    """Validate an optional ``(start, end)`` character span inside the source text."""
    if value is None:
        return None
    try:
        start, end = value  # type: ignore[misc]
    except (TypeError, ValueError):
        raise ValidationError("text offsets must be a (start, end) pair") from None
    if isinstance(start, bool) or isinstance(end, bool):
        raise ValidationError("text offsets must be integers")
    if not isinstance(start, int) or not isinstance(end, int):
        raise ValidationError("text offsets must be integers")
    if start < 0 or end < start:
        raise ValidationError("text offsets must satisfy 0 <= start <= end")
    return (int(start), int(end))


class ConversationEngine:
    """Relational conversation membership through the one Knowledge transaction owner."""

    def __init__(
        self,
        store: "KnowledgeStore",
        *,
        authority: Any,
        retrieval: "RetrievalEngine",
        workspace_id: str,
    ) -> None:
        self._store = store
        self._authority = authority
        self._retrieval = retrieval
        self.workspace_id = str(workspace_id)

    # ── source proof ─────────────────────────────────────────────────────────

    def _prove_source(self, source: SourceRef, context: TrustedCaptureContext) -> SourceRef:
        """Prove provenance, authorization and revocation before anything is written."""
        if not isinstance(source, SourceRef):
            raise ValidationError("source must be a SourceRef")
        if not isinstance(context, TrustedCaptureContext):
            raise ValidationError("capture context must be a TrustedCaptureContext")
        if not self._authority.verify_source(source):
            raise KnowledgeError(
                "denied_unknown_basis",
                f"source {source.event_id}@{source.revision} is not proven",
            )
        issued = self._authority.verify_source_ref(source.event_id, source.revision)
        if issued is None or issued != source:
            # The caller may not hand in provenance the runtime never issued.
            raise KnowledgeError(
                "denied_unknown_basis",
                f"source provenance mismatch for {source.event_id}@{source.revision}",
            )
        authorized = {item.key for item in context.authorized_sources}
        if authorized and source.key not in authorized:
            raise KnowledgeError(
                "unauthorized",
                f"source {source.event_id}@{source.revision} is not authorized",
            )
        if self._authority.source_revoked(source):
            raise KnowledgeError(
                "source_revoked",
                f"source {source.event_id}@{source.revision} is revoked",
            )
        if not source.chat_id:
            # A membership without a chat scope could not be gated on read.
            raise KnowledgeError("denied_unknown_basis", "source carries no chat scope")
        return source

    def _current_source(self, event_id: str, revision: int) -> SourceRef | None:
        """The runtime-issued source, re-read on every render.  Unknown means withheld."""
        try:
            return self._authority.verify_source_ref(str(event_id), int(revision))
        except Exception:  # pragma: no cover - defensive
            return None

    # ── conversations ────────────────────────────────────────────────────────

    def _conversation_row(self, conversation_id: str) -> Any | None:
        return self._store.query_one(
            "SELECT * FROM conversations WHERE conversation_id = ? AND workspace_id = ?",
            (str(conversation_id), self.workspace_id),
        )

    def _resolve_conversation(self, conversation_id: str) -> tuple[Any | None, str | None]:
        """Follow ``merged_into`` redirects.  Returns (row, original id when redirected)."""
        original = str(conversation_id)
        seen: set[str] = set()
        current = original
        for _ in range(16):
            if current in seen:
                return None, None
            seen.add(current)
            row = self._conversation_row(current)
            if row is None:
                return None, None
            target = row["merged_into"]
            if not target:
                return row, (original if original != current else None)
            current = str(target)
        return None, None

    def _create_conversation(
        self,
        *,
        scope_key: str,
        origin: str,
        confidence: float,
        classifier_version: str,
        now_ms: int,
    ) -> str:
        conversation_id = self._store.new_id()
        self._store.execute(
            "INSERT INTO conversations (conversation_id, workspace_id, scope_key, status,"
            " origin, confidence, classifier_version, merged_into, created_ms, updated_ms)"
            " VALUES (?, ?, ?, 'open', ?, ?, ?, NULL, ?, ?)",
            (
                conversation_id,
                self.workspace_id,
                scope_key,
                origin,
                float(confidence),
                str(classifier_version),
                int(now_ms),
                int(now_ms),
            ),
        )
        return conversation_id

    def _member_conversation(self, source: SourceRef) -> str | None:
        """The thread a source revision already belongs to in its own chat."""
        row = self._store.query_one(
            "SELECT conversation_id FROM conversation_memberships"
            " WHERE source_event_id = ? AND source_revision = ? AND channel = ?"
            " AND chat_id = ? ORDER BY created_ms, rowid LIMIT 1",
            (
                str(source.event_id),
                int(source.revision),
                str(source.channel),
                str(source.chat_id),
            ),
        )
        if row is None:
            return None
        resolved, _redirect = self._resolve_conversation(str(row["conversation_id"]))
        return None if resolved is None else str(resolved["conversation_id"])

    # ── memberships ──────────────────────────────────────────────────────────

    def attach_membership(
        self,
        source: SourceRef,
        *,
        context: TrustedCaptureContext,
        conversation_id: str | None = None,
        origin: str = "manual",
        confidence: float = 1.0,
        classifier_version: str = "",
        text_offsets: tuple[int, int] | None = None,
        now_ms: int,
    ) -> ConversationMembershipReceipt:
        """Attach one source revision to a conversation, creating the thread if needed.

        Replaying the same ``(conversation_id, source_event_id, source_revision)``
        changes nothing: the primary key makes membership idempotent by construction.
        """
        origin = _choice(origin, CONVERSATION_ORIGINS, "origin")
        confidence = _confidence(confidence)
        offsets = _offsets(text_offsets)
        proven = self._prove_source(source, context)
        scope_key = source_scope_key(proven)

        with self._store.transaction():
            created = False
            if conversation_id is None:
                conversation_id = self._create_conversation(
                    scope_key=scope_key,
                    origin=origin,
                    confidence=confidence,
                    classifier_version=classifier_version,
                    now_ms=now_ms,
                )
                created = True
            else:
                row, _redirect = self._resolve_conversation(str(conversation_id))
                if row is None:
                    raise KnowledgeError("unresolved", "unknown conversation")
                if str(row["scope_key"]) != scope_key:
                    # A thread may never span chats: membership must not become a bridge.
                    raise KnowledgeError(
                        "unauthorized",
                        "source chat does not match the conversation chat scope",
                    )
                conversation_id = str(row["conversation_id"])

            cursor = self._store.execute(
                "INSERT OR IGNORE INTO conversation_memberships"
                " (conversation_id, source_event_id, source_revision, channel, chat_id,"
                "  author_principal, origin, confidence, classifier_version,"
                "  text_start, text_end, created_ms)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(conversation_id),
                    proven.event_id,
                    int(proven.revision),
                    proven.channel,
                    proven.chat_id,
                    proven.author_principal,
                    origin,
                    float(confidence),
                    str(classifier_version),
                    None if offsets is None else int(offsets[0]),
                    None if offsets is None else int(offsets[1]),
                    int(now_ms),
                ),
            )
            inserted = int(cursor.rowcount or 0)

        return ConversationMembershipReceipt(
            conversation_id=str(conversation_id),
            created_conversation=created,
            new_memberships=inserted,
            origin=origin,
            confidence=float(confidence),
            classifier_version=str(classifier_version),
            text_offsets=offsets,
        )

    # ── explicit reply / quote ───────────────────────────────────────────────

    def record_reference(
        self,
        source: SourceRef,
        *,
        refers_to: SourceRef,
        context: TrustedCaptureContext,
        kind: str = "reply",
        confidence: float = 1.0,
        classifier_version: str = "explicit-reference-v1",
        now_ms: int,
    ) -> ConversationReferenceReceipt:
        """Record an explicit reply/quote as a relation candidate between two threads.

        The two sources keep their own threads.  Nothing here claims they share a topic;
        the edge *is* the whole statement, and a later explicit decision may merge them.
        """
        origin = REFERENCE_ORIGINS.get(str(kind))
        if origin is None:
            raise ValidationError("reference kind must be 'reply' or 'quote'")
        confidence = _confidence(confidence)
        target = self._prove_source(refers_to, context)
        left_source = self._prove_source(source, context)
        if target.key == left_source.key:
            raise ValidationError("a source cannot reference itself")

        with self._store.transaction():
            left = self._thread_for(left_source, context=context, origin=origin,
                                   confidence=confidence,
                                   classifier_version=classifier_version, now_ms=now_ms)
            right = self._thread_for(target, context=context, origin=origin,
                                     confidence=confidence,
                                     classifier_version=classifier_version, now_ms=now_ms)
            relation_created = self._ensure_relation(
                from_conversation_id=left,
                to_conversation_id=right,
                relation=REFERENCE_RELATION,
                origin=origin,
                confidence=confidence,
                classifier_version=classifier_version,
                now_ms=now_ms,
            )
            relations = self._relations_for(left)
        return ConversationReferenceReceipt(
            conversation_id=left,
            related_conversation_id=right,
            relation_created=relation_created,
            relations=relations,
        )

    def _thread_for(
        self,
        source: SourceRef,
        *,
        context: TrustedCaptureContext,
        origin: str,
        confidence: float,
        classifier_version: str,
        now_ms: int,
    ) -> str:
        """The existing thread of this source revision, or a new one holding it."""
        existing = self._member_conversation(source)
        if existing is not None:
            return existing
        receipt = self.attach_membership(
            source,
            context=context,
            origin=origin,
            confidence=confidence,
            classifier_version=classifier_version,
            now_ms=now_ms,
        )
        return receipt.conversation_id

    # ── relations ────────────────────────────────────────────────────────────

    def _ensure_relation(
        self,
        *,
        from_conversation_id: str,
        to_conversation_id: str,
        relation: str,
        origin: str,
        confidence: float,
        classifier_version: str,
        now_ms: int,
    ) -> bool:
        relation = _choice(relation, CONVERSATION_RELATIONS, "relation")
        cursor = self._store.execute(
            "INSERT OR IGNORE INTO conversation_relations"
            " (relation_id, from_conversation_id, to_conversation_id, relation, origin,"
            "  confidence, classifier_version, created_ms)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self._store.new_id(),
                str(from_conversation_id),
                str(to_conversation_id),
                relation,
                origin,
                float(confidence),
                str(classifier_version),
                int(now_ms),
            ),
        )
        return bool(int(cursor.rowcount or 0))

    def _relation_rows(self, conversation_id: str) -> list[Any]:
        return self._store.query(
            "SELECT * FROM conversation_relations"
            " WHERE from_conversation_id = ? OR to_conversation_id = ?"
            " ORDER BY created_ms, rowid",
            (str(conversation_id), str(conversation_id)),
        )

    def _relations_for(self, conversation_id: str) -> tuple[ConversationRelation, ...]:
        return tuple(_relation_of(row) for row in self._relation_rows(conversation_id))

    # ── split and merge ──────────────────────────────────────────────────────

    def split_conversation(
        self,
        conversation_id: str,
        *,
        sources: tuple[SourceRef, ...],
        context: TrustedCaptureContext,
        origin: str = "split",
        confidence: float = 1.0,
        classifier_version: str = "",
        now_ms: int,
    ) -> ConversationSplitReceipt:
        """Branch a new conversation off an existing one without losing the old rows."""
        origin = _choice(origin, CONVERSATION_ORIGINS, "origin")
        confidence = _confidence(confidence)
        if not sources:
            raise ValidationError("a split needs at least one source")
        for source in sources:
            self._prove_source(source, context)

        with self._store.transaction():
            original, _redirect = self._resolve_conversation(str(conversation_id))
            if original is None:
                raise KnowledgeError("unresolved", "unknown conversation")
            scope_key = str(original["scope_key"])
            branch_id = self._create_conversation(
                scope_key=scope_key,
                origin=origin,
                confidence=confidence,
                classifier_version=classifier_version,
                now_ms=now_ms,
            )
            self._ensure_relation(
                from_conversation_id=branch_id,
                to_conversation_id=str(original["conversation_id"]),
                relation="branches_from",
                origin=origin,
                confidence=confidence,
                classifier_version=classifier_version,
                now_ms=now_ms,
            )
            for source in sources:
                # The prior conversation keeps its own membership rows: a split appends.
                self.attach_membership(
                    source,
                    context=context,
                    conversation_id=branch_id,
                    origin=origin,
                    confidence=confidence,
                    classifier_version=classifier_version,
                    now_ms=now_ms,
                )
            relations = self._relations_for(branch_id)
        return ConversationSplitReceipt(
            conversation_id=branch_id,
            branched_from=str(conversation_id),
            origin=origin,
            relations=relations,
        )

    def merge_conversations(
        self,
        source_ids: tuple[str, ...],
        *,
        target_id: str,
        context: TrustedCaptureContext,
        now_ms: int,
    ) -> ConversationMergeReceipt:
        """Redirect retired conversations to a target; nothing is deleted or rewritten."""
        if not source_ids:
            raise ValidationError("a merge needs at least one source conversation")
        if not isinstance(context, TrustedCaptureContext):
            raise ValidationError("capture context must be a TrustedCaptureContext")

        with self._store.transaction():
            target, _redirect = self._resolve_conversation(str(target_id))
            if target is None:
                raise KnowledgeError("unresolved", "unknown merge target")
            target_id = str(target["conversation_id"])
            scope_key = str(target["scope_key"])
            merged: list[str] = []
            for raw_id in source_ids:
                row, _redirect = self._resolve_conversation(str(raw_id))
                if row is None:
                    raise KnowledgeError("unresolved", "unknown conversation")
                current = str(row["conversation_id"])
                if current == target_id:
                    continue
                if str(row["scope_key"]) != scope_key:
                    # Merging across chats would join two audiences into one thread.
                    raise KnowledgeError(
                        "unauthorized", "a merge may not span two chat scopes"
                    )
                self._ensure_relation(
                    from_conversation_id=current,
                    to_conversation_id=target_id,
                    relation="merged_into",
                    origin="merge",
                    confidence=1.0,
                    classifier_version="",
                    now_ms=now_ms,
                )
                self._store.execute(
                    "UPDATE conversations SET status = 'merged', merged_into = ?,"
                    " updated_ms = ? WHERE conversation_id = ?",
                    (target_id, int(now_ms), current),
                )
                merged.append(current)
            relations = self._relations_for(target_id)
        return ConversationMergeReceipt(
            conversation_id=target_id,
            merged_ids=tuple(merged),
            relations=relations,
        )

    # ── reads ────────────────────────────────────────────────────────────────

    def view(self, conversation_id: str, *, context: TrustedReadContext) -> ConversationView:
        """Render a conversation only for a reader the Phase-1 read gate allows.

        The gate is the same one statements use: current policy revision, current chat
        membership, verified recipients.  Every membership row is then re-checked against
        current source authority, current audience and current revocation, because a
        thread must never keep a withdrawn source visible.
        """
        if not isinstance(context, TrustedReadContext):
            raise ValidationError("read context must be a TrustedReadContext")
        requested = str(conversation_id)
        row, redirected_from = self._resolve_conversation(requested)
        if row is None:
            return ConversationView(conversation_id=requested, reason="unknown_conversation")
        resolved_id = str(row["conversation_id"])
        scope_key = str(row["scope_key"])
        if scope_key != context.scope_key():
            # Reading a thread of another chat is refused before any row is touched.
            return ConversationView(
                conversation_id=resolved_id,
                workspace_id=str(row["workspace_id"]),
                scope_key=scope_key,
                status=str(row["status"]),
                merged_into=row["merged_into"],
                redirected_from=redirected_from,
                reason="scope_mismatch",
            )
        decision = self._retrieval.decide(context)
        if not decision.allowed:
            return ConversationView(
                conversation_id=resolved_id,
                workspace_id=str(row["workspace_id"]),
                scope_key=scope_key,
                status=str(row["status"]),
                merged_into=row["merged_into"],
                redirected_from=redirected_from,
                reason=decision.reason,
            )
        recipients = tuple(sorted(set(decision.recipients)))
        kept: list[ConversationMembership] = []
        withheld = 0
        for member_row in self._membership_rows(resolved_id):
            source = self._current_source(
                str(member_row["source_event_id"]), int(member_row["source_revision"])
            )
            if source is None or self._authority.source_revoked(source):
                withheld += 1
                continue
            if not self._audience_allows(source, recipients):
                withheld += 1
                continue
            kept.append(_membership_of(member_row, source))
        return ConversationView(
            conversation_id=resolved_id,
            workspace_id=str(row["workspace_id"]),
            scope_key=scope_key,
            status=str(row["status"]),
            merged_into=row["merged_into"],
            redirected_from=redirected_from,
            memberships=tuple(kept),
            relations=self._relations_for(resolved_id),
            withheld_memberships=withheld,
            reason="ok" if kept else "empty",
        )

    def _membership_rows(self, conversation_id: str) -> list[Any]:
        return self._store.query(
            "SELECT * FROM conversation_memberships WHERE conversation_id = ?"
            " ORDER BY created_ms, rowid",
            (str(conversation_id),),
        )

    def _audience_allows(self, source: SourceRef, recipients: tuple[str, ...]) -> bool:
        """Every verified recipient must be inside the source's own audience."""
        if not recipients:
            return False
        try:
            audience = self._authority.evidence_audience(source, basis="conversation_read")
        except Exception:  # pragma: no cover - defensive
            return False
        if audience is None:
            return False
        status = str(getattr(audience, "status", "unknown"))
        wanted = set(recipients)
        if status == "known":
            members = frozenset(getattr(audience, "members", ()) or ())
            return wanted <= set(members)
        if status == "author_only":
            return wanted <= {source.author_principal}
        return False

    def conversations_for_source(
        self, source: SourceRef, *, context: TrustedReadContext
    ) -> tuple[ConversationView, ...]:
        """Every thread this source revision belongs to, gated per thread."""
        rows = self._store.query(
            "SELECT conversation_id FROM conversation_memberships"
            " WHERE source_event_id = ? AND source_revision = ?"
            " ORDER BY created_ms, rowid",
            (str(source.event_id), int(source.revision)),
        )
        views: list[ConversationView] = []
        for row in rows:
            view = self.view(str(row["conversation_id"]), context=context)
            if view.reason in ("scope_mismatch", "unknown_conversation"):
                continue
            views.append(view)
        return tuple(views)


def _membership_of(row: Any, source: SourceRef) -> ConversationMembership:
    start = row["text_start"]
    end = row["text_end"]
    offsets = None if start is None or end is None else (int(start), int(end))
    return ConversationMembership(
        conversation_id=str(row["conversation_id"]),
        source=source,
        origin=str(row["origin"]),
        confidence=float(row["confidence"]),
        classifier_version=str(row["classifier_version"]),
        text_offsets=offsets,
        created_ms=int(row["created_ms"]),
    )


def _relation_of(row: Any) -> ConversationRelation:
    return ConversationRelation(
        relation=str(row["relation"]),
        from_conversation_id=str(row["from_conversation_id"]),
        to_conversation_id=str(row["to_conversation_id"]),
        origin=str(row["origin"]),
        confidence=float(row["confidence"]),
        classifier_version=str(row["classifier_version"]),
        created_ms=int(row["created_ms"]),
    )
