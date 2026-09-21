"""Private statement engine: source-bound statements with multiple person roles.

A statement is text plus provenance plus an explicit lifecycle.  The text and its
ranking live in ``memory2_nodes`` (reused, never duplicated), the provenance/lifecycle
shell lives in ``memory2_facts`` (existing shared-fact gates keep working) and the new
``knowledge_statements`` row carries the person-knowledge metadata.

Person roles are *edges*: speaker, reported speaker, subject, participant, mentioned.
Audience rows are never derived from those edges.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Final, Iterable

from yeoman_gateway.knowledge._identity import IdentityEngine
from yeoman_gateway.knowledge._reasons import (
    SUPERSESSION_CORRECTION,
    SUPERSESSION_QUALITY_REJECTED,
    SUPERSESSION_UNKNOWN,
)
from yeoman_gateway.knowledge._store import KnowledgeStore
from yeoman_gateway.knowledge.models import (
    SUPERSESSION_REASONS,
    CaptureJobReceipt,
    CaptureJobRecord,
    CaptureResult,
    ChangeReceipt,
    KnowledgeError,
    PersonLinkCandidate,
    SourceRef,
    StatementCandidate,
    StatementPage,
    StatementSummary,
    TrustedAdminContext,
    TrustedCaptureContext,
    ValidationError,
    validate_name,
)

#: Visibility scopes of the reused shared-fact shell.
VISIBILITY_CHAT_SHARED = "chat_shared"
VISIBILITY_PRINCIPALS = "principals"
VISIBILITY_AUTHOR_ONLY = "author_only"

_AUTHOR_ONLY_BASES = frozenset({"owner_private_note", "owner_only", "author_only"})
_UNKNOWN_AUDIENCE_BASES = frozenset({"unknown", "unproven", "denied_unknown_basis"})


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True, slots=True)
class StoredStatement:
    statement_id: str
    status: str
    content: str
    author_principal: str
    scope_key: str
    workspace_id: str
    visibility_scope: str
    group_rule: str
    valid_from_ms: int
    valid_until_ms: int | None
    superseded_by: str | None
    revoked_at_ms: int | None
    extractor_version: str
    unresolved_mentions: tuple[str, ...]
    created_ms: int
    updated_ms: int


class StatementEngine:
    """Capture, inspect and lifecycle-manage statements."""

    def __init__(
        self,
        store: KnowledgeStore,
        *,
        identity: IdentityEngine,
        authority: Any,
        policy: Any,
        workspace_id: str,
        retention_ms: int | None = None,
    ) -> None:
        self._store = store
        self._identity = identity
        self._authority = authority
        self._policy = policy
        self.workspace_id = str(workspace_id)
        self.retention_ms = retention_ms

    # ── capture ──────────────────────────────────────────────────────────────

    def capture(
        self, candidate: StatementCandidate, *, context: TrustedCaptureContext
    ) -> CaptureResult:
        """Validate and persist one statement.  All-or-nothing, idempotent per source."""
        self._policy.require_capture(context)
        if not context.authorized:
            raise KnowledgeError("unauthorized", "capture is not authorized")

        sources = self._verify_sources(candidate.sources, context)
        audience, visibility, group_rule, snapshot_id, allowed = self._resolve_audience(
            sources, context
        )
        if visibility == VISIBILITY_AUTHOR_ONLY:
            allowed = frozenset()
            audience = frozenset()

        primary = sources[0]
        scope_key = _scope_key(primary)
        dedupe_key = _dedupe_key(
            scope_key=scope_key,
            source=primary,
            content=candidate.content,
            extractor_version=candidate.extractor_version,
            audience=audience,
            visibility=visibility,
        )

        existing = self._store.query_one(
            "SELECT statement_id FROM knowledge_statements WHERE dedupe_key = ?",
            (dedupe_key,),
        )
        if existing is not None:
            return CaptureResult(statement_ids=(str(existing["statement_id"]),))

        links = self._validate_people(candidate, context, sources)
        rejected: list[tuple[str, str]] = []

        statement_id = _stable_id("stmt", dedupe_key)
        content_hash = _content_hash(statement_id, candidate.content)
        ts = now_ms()
        valid_from = int(primary.occurred_at_ms or ts)
        valid_until = candidate.valid_until_ms
        if valid_until is None and self.retention_ms:
            valid_until = valid_from + int(self.retention_ms)

        speaker = self._identity.person_id_for_principal(primary.author_principal)
        if speaker is not None:
            speaker = self._identity.canonical_id(speaker)

        self._insert_node(
            statement_id=statement_id,
            content=candidate.content,
            content_hash=content_hash,
            channel=primary.channel,
            chat_id=primary.chat_id,
            scope_key=scope_key,
            speaker=speaker,
            kind=candidate.kind,
            sector=candidate.sector,
            confidence=candidate.confidence,
            source_event_id=primary.event_id,
            ts=ts,
        )
        self._insert_statement_row(
            statement_id=statement_id,
            scope_key=scope_key,
            author_principal=primary.author_principal,
            speaker=speaker,
            status="assertion",
            visibility=visibility,
            group_rule=group_rule,
            channel=primary.channel,
            chat_id=primary.chat_id,
            snapshot_id=snapshot_id,
            valid_from_ms=valid_from,
            valid_until_ms=valid_until,
            extractor_version=candidate.extractor_version,
            content_hash=content_hash,
            unresolved=candidate.unresolved_mentions,
            dedupe_key=dedupe_key,
            ts=ts,
        )
        self._insert_fact_shell(
            statement_id=statement_id,
            scope_key=scope_key,
            author_principal=primary.author_principal,
            status="assertion",
            visibility=visibility,
            group_rule=group_rule,
            snapshot_id=snapshot_id,
            valid_from_ms=valid_from,
            valid_until_ms=valid_until,
            extractor_version=candidate.extractor_version,
            audience=audience,
            allowed=allowed,
            sources=sources,
            ts=ts,
        )
        self._insert_sources(statement_id, sources, ts=ts)
        for link in links:
            self._insert_person_link(statement_id, link, ts=ts)
        if speaker is not None:
            self._insert_person_link(
                statement_id,
                PersonLinkCandidate(
                    person_id=speaker,
                    role="speaker",
                    source=primary,
                    attribution="transport",
                ),
                ts=ts,
            )
        # `speaker` may already have been proposed by the extractor with the same
        # evidence; the primary key makes the second insert a no-op.
        link_rows = int(
            self._store.scalar(
                "SELECT COUNT(*) FROM knowledge_statement_people WHERE statement_id = ?",
                (statement_id,),
            )
            or 0
        )
        self.audit(
            statement_id,
            operation="capture",
            actor=context.actor_principal,
            reason=context.capture_basis,
            detail={"links": link_rows, "sources": len(sources), "rejected": len(rejected)},
            ts=ts,
        )
        return CaptureResult(statement_ids=(statement_id,), rejected=tuple(rejected))

    def _verify_sources(
        self, sources: tuple[SourceRef, ...], context: TrustedCaptureContext
    ) -> tuple[SourceRef, ...]:
        authorized = {item.key: item for item in context.authorized_sources}
        verified: list[SourceRef] = []
        for source in sources:
            proven = self._authority.verify_source(source)
            if not proven:
                raise KnowledgeError(
                    "denied_unknown_basis",
                    f"source {source.event_id}@{source.revision} is not proven",
                )
            issued = self._authority.verify_source_ref(source.event_id, source.revision)
            if issued is None or issued != source:
                # The candidate may not hand in provenance the runtime never issued.
                raise KnowledgeError(
                    "denied_unknown_basis",
                    f"source provenance mismatch for {source.event_id}@{source.revision}",
                )
            if authorized and source.key not in authorized:
                raise KnowledgeError(
                    "unauthorized",
                    f"source {source.event_id}@{source.revision} is not authorized",
                )
            if self._authority.source_revoked(source):
                # Fail closed: a revoked revision can never publish new statements.
                self.invalidate_source_internal(source, reason="revoked_before_capture")
                raise KnowledgeError(
                    "source_revoked",
                    f"source {source.event_id}@{source.revision} is revoked",
                )
            verified.append(source)
        return tuple(verified)

    def effective_read_principals(self, statement_id: str) -> tuple[frozenset[str], str]:
        """Who may read this statement on the shared-fact shell, and its visibility.

        Only ``author_only`` adds its author; every other scope relies on explicit
        audience rows, which are never derived from person roles.
        """
        row = self._store.query_one(
            "SELECT visibility_scope, author_principal FROM knowledge_statements"
            " WHERE statement_id = ?",
            (str(statement_id),),
        )
        if row is None:
            return frozenset(), "author_only"
        visibility = str(row["visibility_scope"])
        if visibility == "author_only":
            return frozenset({str(row["author_principal"])}), visibility
        rows = self._store.query(
            "SELECT principal_id FROM knowledge_statement_principals"
            " WHERE statement_id = ? AND role = 'audience'",
            (str(statement_id),),
        )
        return frozenset(str(item["principal_id"]) for item in rows), visibility

    def _resolve_audience(
        self, sources: tuple[SourceRef, ...], context: TrustedCaptureContext
    ) -> tuple[frozenset[str], str, str, str | None, frozenset[str]]:
        """Audience comes from archived evidence only; the model never supplies it.

        Multiple sources intersect, never union.  An unknown audience fails closed and
        is reported as ``denied_unknown_basis`` instead of silently becoming ``normal``.
        """
        audiences: list[frozenset[str]] = []
        snapshot_id: str | None = None
        explicit: frozenset[str] = frozenset()
        for source in sources:
            resolution = self._authority.evidence_audience(source, basis=context.capture_basis)
            if resolution is None or resolution.status == "unknown":
                raise KnowledgeError(
                    "denied_unknown_basis",
                    f"no proven audience for source {source.event_id}",
                )
            if resolution.status == "author_only":
                return frozenset(), VISIBILITY_AUTHOR_ONLY, "author_only", resolution.snapshot_id, (
                    frozenset()
                )
            if not resolution.members:
                raise KnowledgeError(
                    "denied_unknown_basis",
                    f"empty proven audience for source {source.event_id}",
                )
            audiences.append(frozenset(resolution.members))
            snapshot_id = snapshot_id or resolution.snapshot_id
            if resolution.explicit:
                explicit = explicit | frozenset(resolution.allowed)

        effective = frozenset(audiences[0])
        for members in audiences[1:]:
            effective &= members
        if not effective:
            raise KnowledgeError(
                "denied_unknown_basis",
                "sources have no common audience",
            )
        visibility = VISIBILITY_PRINCIPALS if explicit else VISIBILITY_CHAT_SHARED
        group_rule = "explicit_principals" if explicit else "chat_members_at_source"
        return effective, visibility, group_rule, snapshot_id, explicit

    def _validate_people(
        self,
        candidate: StatementCandidate,
        context: TrustedCaptureContext,
        sources: tuple[SourceRef, ...],
    ) -> tuple[PersonLinkCandidate, ...]:
        """Every link must name a real person and one of *this* statement's sources.

        A model may only use person ids the runtime offered; a foreign or invented uuid
        is rejected.  The transport speaker is set from the message, so a proposal that
        claims a different speaker with transport attribution is dropped.
        """
        source_keys = {item.key for item in sources}
        speaker_principal = sources[0].author_principal
        speaker_person = self._identity.person_id_for_principal(speaker_principal)
        out: list[PersonLinkCandidate] = []
        for link in candidate.people:
            if link.source.key not in source_keys:
                raise KnowledgeError(
                    "invalid_input",
                    "person link references a source that is not part of the statement",
                )
            person = self._identity.get_person(link.person_id)
            if person is None:
                raise KnowledgeError(
                    "invalid_input",
                    f"person link references an unknown person: {link.person_id}",
                )
            if link.role == "speaker":
                if link.attribution == "transport":
                    if speaker_person is None or (
                        self._identity.canonical_id(link.person_id)
                        != self._identity.canonical_id(speaker_person)
                    ):
                        raise KnowledgeError(
                            "unauthorized",
                            "transport speaker must be the author of the source",
                        )
                continue  # the engine writes the transport speaker edge itself
            out.append(link)
        return tuple(out)

    def _insert_node(
        self,
        *,
        statement_id: str,
        content: str,
        content_hash: str,
        channel: str,
        chat_id: str,
        scope_key: str,
        speaker: str | None,
        kind: str,
        sector: str,
        confidence: float,
        source_event_id: str,
        ts: int,
    ) -> None:
        iso = _iso(ts)
        self._store.execute(
            """
            INSERT INTO memory2_nodes (
                id, workspace_id, scope_type, scope_key, channel, chat_id, sender_id,
                contact_id, sector, kind, content, content_norm, content_hash, salience,
                confidence, source, source_message_id, source_role, language, meta_json,
                created_at, updated_at, last_accessed_at, valid_from, valid_to, is_deleted
            ) VALUES (?, ?, 'chat', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'statement', ?, NULL,
                      NULL, '{}', ?, ?, NULL, ?, NULL, 0)
            """,
            (
                statement_id,
                self.workspace_id,
                scope_key,
                channel,
                chat_id,
                speaker,
                speaker,
                sector,
                kind,
                content,
                " ".join(content.split()).strip().lower(),
                content_hash,
                max(0.0, min(1.0, float(confidence))),
                max(0.0, min(1.0, float(confidence))),
                source_event_id,
                iso,
                iso,
                iso,
            ),
        )
        self._store.execute(
            "INSERT INTO memory2_nodes_fts (entry_id, content) VALUES (?, ?)",
            (statement_id, content),
        )
        self._store.execute(
            "INSERT INTO knowledge_meta (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO NOTHING",
            (f"statement_created:{statement_id}", iso),
        )

    def _insert_statement_row(
        self,
        *,
        statement_id: str,
        scope_key: str,
        author_principal: str,
        speaker: str | None,
        status: str,
        visibility: str,
        group_rule: str,
        channel: str,
        chat_id: str,
        snapshot_id: str | None,
        valid_from_ms: int,
        valid_until_ms: int | None,
        extractor_version: str,
        content_hash: str,
        unresolved: tuple[str, ...],
        dedupe_key: str,
        ts: int,
    ) -> None:
        self._store.execute(
            """
            INSERT INTO knowledge_statements (
                statement_id, workspace_id, scope_key, author_principal, speaker_person_id,
                status, visibility_scope, group_rule, source_chat_id, source_channel,
                audience_snapshot_id, valid_from_ms, valid_until_ms, superseded_by,
                revoked_at_ms, extractor_version, content_hash, unresolved_mentions_json,
                dedupe_key, created_ms, updated_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?)
            """,
            (
                statement_id,
                self.workspace_id,
                scope_key,
                author_principal,
                speaker,
                status,
                visibility,
                group_rule,
                chat_id,
                channel,
                snapshot_id,
                int(valid_from_ms),
                None if valid_until_ms is None else int(valid_until_ms),
                extractor_version,
                content_hash,
                json.dumps(list(unresolved), separators=(",", ":")),
                dedupe_key,
                ts,
                ts,
            ),
        )

    def _insert_fact_shell(
        self,
        *,
        statement_id: str,
        scope_key: str,
        author_principal: str,
        status: str,
        visibility: str,
        group_rule: str,
        snapshot_id: str | None,
        valid_from_ms: int,
        valid_until_ms: int | None,
        extractor_version: str,
        audience: frozenset[str],
        allowed: frozenset[str],
        sources: tuple[SourceRef, ...],
        ts: int,
    ) -> None:
        """Reuse the existing shared-fact shell so the existing gates keep working."""
        self._store.execute(
            """
            INSERT INTO memory2_facts (
                fact_id, workspace_id, chat_scope_key, author_principal, assertion_status,
                visibility_scope, group_rule, audience_snapshot_id, valid_from_ms,
                valid_until_ms, superseded_by, revoked_at_ms, extractor_version,
                created_ms, updated_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?)
            ON CONFLICT(fact_id) DO UPDATE SET
                assertion_status = excluded.assertion_status,
                visibility_scope = excluded.visibility_scope,
                group_rule = excluded.group_rule,
                audience_snapshot_id = excluded.audience_snapshot_id,
                valid_until_ms = excluded.valid_until_ms,
                updated_ms = excluded.updated_ms
            """,
            (
                statement_id,
                self.workspace_id,
                scope_key,
                author_principal,
                status,
                visibility,
                group_rule,
                snapshot_id,
                int(valid_from_ms),
                None if valid_until_ms is None else int(valid_until_ms),
                extractor_version,
                ts,
                ts,
            ),
        )
        for principal in sorted(audience):
            self._store.execute(
                "INSERT OR IGNORE INTO memory2_fact_principals (fact_id, principal_id, role)"
                " VALUES (?, ?, 'audience')",
                (statement_id, principal),
            )
            self._store.execute(
                "INSERT OR IGNORE INTO knowledge_statement_principals"
                " (statement_id, principal_id, role) VALUES (?, ?, 'audience')",
                (statement_id, principal),
            )
        for principal in sorted(allowed):
            self._store.execute(
                "INSERT OR IGNORE INTO memory2_fact_principals (fact_id, principal_id, role)"
                " VALUES (?, ?, 'allowed')",
                (statement_id, principal),
            )
            self._store.execute(
                "INSERT OR IGNORE INTO knowledge_statement_principals"
                " (statement_id, principal_id, role) VALUES (?, ?, 'allowed')",
                (statement_id, principal),
            )
        for source in sources:
            self._store.execute(
                """
                INSERT OR IGNORE INTO memory2_fact_sources (
                    fact_id, source_event_id, source_revision, source_trace_id,
                    author_principal, source_channel, source_chat_id, occurred_ms
                ) VALUES (?, ?, ?, '', ?, ?, ?, ?)
                """,
                (
                    statement_id,
                    source.event_id,
                    int(source.revision),
                    source.author_principal,
                    source.channel,
                    source.chat_id,
                    int(source.occurred_at_ms),
                ),
            )

    def _insert_sources(
        self, statement_id: str, sources: tuple[SourceRef, ...], *, ts: int
    ) -> None:
        for source in sources:
            audience = self._authority.evidence_audience(source, basis="")
            members = None if audience is None else sorted(audience.members)
            snapshot = None if audience is None else audience.snapshot_id
            self._store.execute(
                """
                INSERT OR IGNORE INTO knowledge_statement_sources (
                    statement_id, event_id, revision, channel, chat_id, author_principal,
                    occurred_at_ms, source_audience_json, snapshot_id, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
                """,
                (
                    statement_id,
                    source.event_id,
                    int(source.revision),
                    source.channel,
                    source.chat_id,
                    source.author_principal,
                    int(source.occurred_at_ms),
                    None if members is None else json.dumps(members, separators=(",", ":")),
                    snapshot,
                ),
            )

    def _insert_person_link(
        self, statement_id: str, link: PersonLinkCandidate, *, ts: int
    ) -> bool:
        cursor = self._store.execute(
            """
            INSERT OR IGNORE INTO knowledge_statement_people (
                statement_id, person_id, role, evidence_source_id, evidence_revision,
                attribution, created_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                statement_id,
                link.person_id,
                link.role,
                link.source.event_id,
                int(link.source.revision),
                link.attribution,
                ts,
            ),
        )
        return bool(cursor.rowcount)

    def audit(
        self,
        statement_id: str,
        *,
        operation: str,
        actor: str = "",
        evidence_ref: str = "",
        reason: str = "",
        detail: dict[str, Any] | None = None,
        ts: int | None = None,
    ) -> None:
        self._store.execute(
            "INSERT INTO knowledge_statement_audit (statement_id, operation, actor_principal,"
            " evidence_ref, reason, detail_json, created_ms) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                str(statement_id),
                str(operation),
                str(actor or ""),
                str(evidence_ref or ""),
                str(reason or ""),
                json.dumps(detail or {}, separators=(",", ":"), sort_keys=True),
                int(ts if ts is not None else now_ms()),
            ),
        )

    # ── reading ──────────────────────────────────────────────────────────────

    def get_statement(self, statement_id: str) -> StoredStatement | None:
        row = self._store.query_one(
            "SELECT s.*, n.content AS content FROM knowledge_statements s"
            " JOIN memory2_nodes n ON n.id = s.statement_id"
            " WHERE s.statement_id = ?",
            (str(statement_id),),
        )
        if row is None:
            return None
        return StoredStatement(
            statement_id=str(row["statement_id"]),
            status=str(row["status"]),
            content=str(row["content"] or ""),
            author_principal=str(row["author_principal"]),
            scope_key=str(row["scope_key"]),
            workspace_id=str(row["workspace_id"]),
            visibility_scope=str(row["visibility_scope"]),
            group_rule=str(row["group_rule"]),
            valid_from_ms=int(row["valid_from_ms"]),
            valid_until_ms=None if row["valid_until_ms"] is None else int(row["valid_until_ms"]),
            superseded_by=None if row["superseded_by"] is None else str(row["superseded_by"]),
            revoked_at_ms=None if row["revoked_at_ms"] is None else int(row["revoked_at_ms"]),
            extractor_version=str(row["extractor_version"]),
            unresolved_mentions=tuple(
                str(item) for item in json.loads(str(row["unresolved_mentions_json"] or "[]"))
            ),
            created_ms=int(row["created_ms"]),
            updated_ms=int(row["updated_ms"]),
        )

    def sources_of(self, statement_id: str) -> tuple[tuple[SourceRef, str], ...]:
        rows = self._store.query(
            "SELECT * FROM knowledge_statement_sources WHERE statement_id = ?"
            " ORDER BY event_id, revision",
            (str(statement_id),),
        )
        out: list[tuple[SourceRef, str]] = []
        for row in rows:
            out.append(
                (
                    SourceRef(
                        event_id=str(row["event_id"]),
                        revision=int(row["revision"]),
                        channel=str(row["channel"]),
                        chat_id=str(row["chat_id"]),
                        author_principal=str(row["author_principal"]),
                        occurred_at_ms=int(row["occurred_at_ms"]),
                    ),
                    str(row["status"]),
                )
            )
        return tuple(out)

    def summary(self, statement_id: str, *, include_content: bool = True) -> StatementSummary:
        record = self.get_statement(statement_id)
        if record is None:
            raise KnowledgeError("unresolved", f"unknown statement: {statement_id}")
        links = self._identity.people_for_statement(record.statement_id)
        people = tuple(
            PersonLinkCandidate(
                person_id=person_id,
                role=role,
                source=self._source_for(record.statement_id, event_id, revision),
                attribution="transport" if role == "speaker" else "extracted",
            )
            for person_id, role, event_id, revision in links
        )
        return StatementSummary(
            statement_id=record.statement_id,
            status=record.status,
            content=record.content if include_content else None,
            sources=tuple(item[0] for item in self.sources_of(record.statement_id)),
            people=people,
            created_ms=record.created_ms,
            updated_ms=record.updated_ms,
            valid_until_ms=record.valid_until_ms,
        )

    def _source_for(self, statement_id: str, event_id: str, revision: int) -> SourceRef:
        row = self._store.query_one(
            "SELECT * FROM knowledge_statement_sources WHERE statement_id = ?"
            " AND event_id = ? AND revision = ?",
            (str(statement_id), str(event_id), int(revision)),
        )
        if row is not None:
            return SourceRef(
                event_id=str(row["event_id"]),
                revision=int(row["revision"]),
                channel=str(row["channel"] or "unknown"),
                chat_id=str(row["chat_id"] or "unknown"),
                author_principal=str(row["author_principal"]),
                occurred_at_ms=int(row["occurred_at_ms"]),
            )
        live = self._authority.verify_source_ref(event_id, revision)
        if live is None:
            raise KnowledgeError(
                "denied_unknown_basis",
                f"statement {statement_id} lost its source evidence",
            )
        return live

    def list_statement_ids(
        self,
        *,
        statuses: Iterable[str] | None = None,
        before_ms: int | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> tuple[tuple[str, ...], str | None]:
        if limit < 1 or limit > 100:
            raise ValidationError("page limit must be within 1..100")
        clauses: list[str] = ["s.workspace_id = ?"]
        params: list[Any] = [self.workspace_id]
        if statuses:
            values = sorted(set(statuses))
            clauses.append(f"s.status IN ({','.join('?' for _ in values)})")
            params.extend(values)
        if before_ms is not None:
            clauses.append("s.created_ms < ?")
            params.append(int(before_ms))
        offset = 0
        if cursor:
            offset = _decode_cursor(cursor, params)
        where = " AND ".join(clauses)
        rows = self._store.query(
            f"SELECT s.statement_id FROM knowledge_statements s WHERE {where}"
            " ORDER BY s.created_ms DESC, s.statement_id DESC LIMIT ?",
            (*params, int(limit) + 1),
        )
        ids = [str(row["statement_id"]) for row in rows]
        next_cursor = None
        if len(ids) > limit:
            ids = ids[:limit]
            next_cursor = _encode_cursor(params, offset + limit)
        return tuple(ids), next_cursor

    def active_statement_ids_for_event(self, event_id: str, revision: int | None = None) -> tuple[str, ...]:
        clauses = ["src.event_id = ?", "src.status = 'active'"]
        params: list[Any] = [str(event_id)]
        if revision is not None:
            clauses.append("src.revision = ?")
            params.append(int(revision))
        rows = self._store.query(
            "SELECT DISTINCT s.statement_id FROM knowledge_statement_sources src"
            " JOIN knowledge_statements s ON s.statement_id = src.statement_id"
            f" WHERE {' AND '.join(clauses)}",
            tuple(params),
        )
        return tuple(str(row["statement_id"]) for row in rows)

    # ── lifecycle ────────────────────────────────────────────────────────────

    def invalidate_source_internal(
        self, source: SourceRef, *, actor: str = "", reason: str = "revoked"
    ) -> ChangeReceipt:
        """Invalidate one source revision on behalf of an already-authorized caller.

        Used by the capture path itself (after its own authorization check) and by
        trusted lifecycle handlers that were authorized elsewhere.  Revocation is
        idempotent and always wins: a revoked revision never becomes usable again.
        """
        return self._invalidate(source, actor=actor, reason=reason)

    def invalidate_source(
        self, source: SourceRef, *, context: TrustedCaptureContext, reason: str = "revoked"
    ) -> ChangeReceipt:
        """Revoke one source revision: every dependent statement becomes unreadable.

        A statement whose *only* basis was this source is redacted.  A statement with
        further sources is superseded and locked until it is regenerated without the
        revoked basis - it is never silently re-authorized.
        """
        self._policy.require_capture(context)
        return self._invalidate(source, actor=context.actor_principal, reason=reason)

    def _invalidate(self, source: SourceRef, *, actor: str, reason: str) -> ChangeReceipt:
        operation_id = self._store.new_id()
        ts = self._store.now_ms()
        affected = self._store.query(
            "SELECT statement_id, status FROM knowledge_statement_sources"
            " WHERE event_id = ? AND revision = ?",
            (source.event_id, int(source.revision)),
        )
        changed: list[str] = []
        for row in affected:
            statement_id = str(row["statement_id"])
            if str(row["status"]) == "revoked":
                # Already invalidated: revocation is idempotent, but the source never
                # becomes usable again.
                continue
            remaining = int(
                self._store.scalar(
                    "SELECT COUNT(*) FROM knowledge_statement_sources"
                    " WHERE statement_id = ? AND status = 'active'"
                    " AND NOT (event_id = ? AND revision = ?)",
                    (statement_id, source.event_id, int(source.revision)),
                )
                or 0
            )
            self._store.execute(
                "UPDATE knowledge_statement_sources SET status = 'revoked'"
                " WHERE statement_id = ? AND event_id = ? AND revision = ?",
                (statement_id, source.event_id, int(source.revision)),
            )
            self._store.execute(
                "DELETE FROM memory2_fact_sources WHERE fact_id = ?"
                " AND source_event_id = ? AND source_revision = ?",
                (statement_id, source.event_id, int(source.revision)),
            )
            if remaining:
                # The evidence behind one revision was withdrawn, so the statement stops
                # being a current value.  That is *not* a proven state change or a
                # correction, and the reason is deliberately not guessed.
                self._set_status(
                    statement_id,
                    status="superseded",
                    ts=ts,
                    superseded_by=None,
                    supersession_reason=SUPERSESSION_UNKNOWN,
                )
            else:
                self._redact(statement_id, ts=ts)
            self.audit(
                statement_id,
                operation="invalidate_source",
                actor=actor,
                evidence_ref=f"{source.event_id}@{source.revision}",
                reason=reason if remaining == 0 else f"{reason}:multi_source",
                ts=ts,
            )
            changed.append(statement_id)
        cancelled = self.cancel_jobs_for_event(source.event_id, revision=source.revision, ts=ts)
        # Tell the proof owner: from now on this revision can never publish again.
        self._authority.mark_source_revoked(source)
        revision = self._store.bump_identity_revision()
        epoch = self._store.bump_acl_epoch()
        return ChangeReceipt(
            operation_id=operation_id,
            identity_revision=revision,
            acl_epoch=epoch,
            changed_ids=tuple(changed) + tuple(cancelled),
        )

    def invalidate_event_ids(
        self, event_ids: Iterable[str], *, actor: str = "", reason: str = "source_revoked"
    ) -> tuple[str, ...]:
        """Apply an already-projected source revocation to derived statements.

        The provider revocation itself was authorized and projected by the journal owner
        before this runs; this only applies it to what was derived.  It needs no capture
        context for that reason, and it is idempotent: a second call changes nothing.
        """
        wanted = [str(item) for item in event_ids if str(item)]
        if not wanted:
            return ()
        placeholders = ",".join("?" for _ in wanted)
        rows = self._store.query(
            "SELECT DISTINCT statement_id, event_id, revision FROM knowledge_statement_sources"
            f" WHERE event_id IN ({placeholders})",
            tuple(wanted),
        )
        changed: list[str] = []
        for row in rows:
            statement_id = str(row["statement_id"])
            try:
                source = self._source_for(
                    statement_id, str(row["event_id"]), int(row["revision"])
                )
            except KnowledgeError:  # pragma: no cover - defensive
                continue
            receipt = self._invalidate(source, actor=str(actor), reason=str(reason))
            changed.extend(str(item) for item in receipt.changed_ids)
        return tuple(dict.fromkeys(changed))

    def correct_statement(
        self,
        statement_id: str,
        replacement: StatementCandidate,
        *,
        expected_source: SourceRef,
        context: TrustedCaptureContext,
    ) -> ChangeReceipt:
        """Explicit correction inside the same authorized context.

        Only the named statement is superseded; contradictory statements from other
        sources are left alone.
        """
        self._policy.require_capture(context)
        original = self.get_statement(statement_id)
        if original is None:
            raise KnowledgeError("unresolved", f"unknown statement: {statement_id}")
        source_rows = {item[0].key: item for item in self.sources_of(statement_id)}
        if expected_source.key not in source_rows:
            raise KnowledgeError(
                "invalid_input",
                "expected_source is not a source of this statement",
            )
        if original.status in ("revoked",):
            raise KnowledgeError("source_revoked", "cannot correct a revoked statement")
        ts = now_ms()
        result = self.capture(replacement, context=context)
        new_id = result.statement_ids[0]
        if new_id == statement_id:
            # The replacement is byte-identical to the row it would supersede: that is
            # not a correction, and silently "succeeding" would hide a caller mistake.
            raise KnowledgeError(
                "identity_conflict",
                "replacement is identical to the statement it should correct",
            )
        if self._would_cycle(statement_id, new_id):
            raise KnowledgeError("invalid_input", "supersession would create a cycle")
        self._set_status(
            statement_id,
            status="superseded",
            ts=ts,
            superseded_by=new_id,
            supersession_reason=SUPERSESSION_CORRECTION,
        )
        self._store.execute(
            "UPDATE knowledge_statements SET superseded_by = ?, updated_ms = ?"
            " WHERE statement_id = ? AND superseded_by IS NULL",
            (new_id, ts, statement_id),
        )
        self.audit(
            statement_id,
            operation="correct",
            actor=context.actor_principal,
            evidence_ref=f"{expected_source.event_id}@{expected_source.revision}",
            reason="explicit_correction",
            detail={"replacement": new_id},
            ts=ts,
        )
        revision = self._store.bump_identity_revision()
        epoch = self._store.bump_acl_epoch()
        return ChangeReceipt(
            operation_id=self._store.new_id(),
            identity_revision=revision,
            acl_epoch=epoch,
            changed_ids=(statement_id, new_id),
        )

    def confirm_statement(
        self,
        statement_id: str,
        *,
        expected_source: SourceRef,
        context: TrustedAdminContext,
        evidence_ref: str,
    ) -> ChangeReceipt:
        """Mark a statement confirmed.  Model confidence never reaches this method."""
        if not context.owner:
            raise KnowledgeError("unauthorized", "admin context lacks owner authority")
        self._policy.require_admin(context)
        record = self.get_statement(statement_id)
        if record is None:
            raise KnowledgeError("unresolved", f"unknown statement: {statement_id}")
        if record.status not in ("assertion", "confirmed"):
            raise KnowledgeError("identity_conflict", "statement is not confirmable")
        if expected_source.key not in {item[0].key for item in self.sources_of(statement_id)}:
            raise KnowledgeError("invalid_input", "expected_source is not a source")
        verified = self._authority.verify_evidence_ref(evidence_ref)
        ts = now_ms()
        self._set_status(statement_id, status="confirmed", ts=ts)
        self.audit(
            statement_id,
            operation="confirm",
            actor=context.actor_principal,
            evidence_ref=verified,
            reason="explicit_confirmation",
            ts=ts,
        )
        return ChangeReceipt(
            operation_id=self._store.new_id(),
            identity_revision=self._store.bump_identity_revision(),
            acl_epoch=self._store.bump_acl_epoch(),
            changed_ids=(statement_id,),
        )

    def erase_statement(
        self,
        statement_id: str,
        *,
        expected_source: SourceRef,
        context: TrustedAdminContext,
    ) -> ChangeReceipt:
        """Erase payload from text, FTS, embeddings and derived caches.

        Only an id-only tombstone and a content-free audit row survive.  Whether the
        source archive or a backup still holds a copy is owned by those subsystems and
        is reported by ``remaining_copies`` instead of being claimed here.
        """
        if not context.owner:
            raise KnowledgeError("unauthorized", "admin context lacks owner authority")
        self._policy.require_admin(context)
        record = self.get_statement(statement_id)
        if record is None:
            raise KnowledgeError("unresolved", f"unknown statement: {statement_id}")
        if expected_source.key not in {item[0].key for item in self.sources_of(statement_id)}:
            raise KnowledgeError("invalid_input", "expected_source is not a source")
        ts = self._store.now_ms()
        self._redact(statement_id, ts=ts)
        # Overwrite the shell in place: one content-free tombstone, no residual payload.
        self._store.execute(
            "UPDATE knowledge_statements SET speaker_person_id = NULL, status = 'revoked',"
            " visibility_scope = 'author_only', group_rule = 'author_only',"
            " source_chat_id = '', source_channel = '', audience_snapshot_id = NULL,"
            " valid_until_ms = NULL, superseded_by = NULL, revoked_at_ms = ?,"
            " content_hash = '', unresolved_mentions_json = '[]',"
            " dedupe_key = 'tombstone:' || statement_id, updated_ms = ?"
            " WHERE statement_id = ?",
            (ts, ts, str(statement_id)),
        )
        self._store.execute(
            "DELETE FROM knowledge_statement_people WHERE statement_id = ?",
            (str(statement_id),),
        )
        self._store.execute(
            "UPDATE knowledge_statement_sources SET status = 'revoked',"
            " source_audience_json = NULL WHERE statement_id = ?",
            (str(statement_id),),
        )
        self._store.execute(
            "UPDATE memory2_facts SET assertion_status = 'revoked', revoked_at_ms = ?,"
            " visibility_scope = 'author_only', group_rule = 'author_only', updated_ms = ?"
            " WHERE fact_id = ?",
            (ts, ts, str(statement_id)),
        )
        self._store.execute(
            "DELETE FROM memory2_fact_principals WHERE fact_id = ?", (str(statement_id),)
        )
        self._store.execute(
            "DELETE FROM knowledge_statement_principals WHERE statement_id = ?",
            (str(statement_id),),
        )
        self._store.execute(
            "UPDATE memory2_nodes SET content = '', content_norm = '', content_hash = '',"
            " is_deleted = 1, updated_at = ? WHERE id = ?",
            (_iso(ts), str(statement_id)),
        )
        self._store.execute(
            "DELETE FROM memory2_nodes_fts WHERE entry_id = ?", (str(statement_id),)
        )
        self._store.execute(
            "DELETE FROM memory2_embeddings WHERE entry_id = ?", (str(statement_id),)
        )
        self._store.execute(
            "DELETE FROM knowledge_meta WHERE key = ?", (f"statement_created:{statement_id}",)
        )
        self.audit(
            statement_id,
            operation="erase",
            actor=context.actor_principal,
            evidence_ref=context.authorization_ref,
            reason="erase_requested",
            ts=ts,
        )
        return ChangeReceipt(
            operation_id=self._store.new_id(),
            identity_revision=self._store.bump_identity_revision(),
            acl_epoch=self._store.bump_acl_epoch(),
            changed_ids=(str(statement_id),),
        )

    def _would_cycle(self, statement_id: str, replacement_id: str) -> bool:
        seen: set[str] = set()
        current = replacement_id
        while current and current not in seen:
            if current == statement_id:
                return True
            seen.add(current)
            row = self._store.query_one(
                "SELECT superseded_by FROM knowledge_statements WHERE statement_id = ?",
                (current,),
            )
            current = "" if row is None or row["superseded_by"] is None else str(row["superseded_by"])
        return False

    def _set_status(
        self,
        statement_id: str,
        *,
        status: str,
        ts: int,
        superseded_by: str | None = None,
        supersession_reason: str = "",
    ) -> None:
        """Write status, successor and machine-readable reason as one unit.

        The reason is only meaningful for ``superseded``; it is written in the same
        UPDATE as the status, so no reader can ever observe a superseded row without its
        reason and no crash can split the two.
        """
        if supersession_reason and supersession_reason not in SUPERSESSION_REASONS:
            raise ValidationError(
                f"supersession_reason must be one of {SUPERSESSION_REASONS}"
            )
        self._store.execute(
            "UPDATE knowledge_statements SET status = ?,"
            " superseded_by = COALESCE(?, superseded_by),"
            " supersession_reason = CASE WHEN ? = 'superseded' THEN ?"
            "   ELSE supersession_reason END,"
            " revision = revision + 1,"
            " revoked_at_ms = CASE WHEN ? = 'revoked' THEN ? ELSE revoked_at_ms END,"
            " updated_ms = ? WHERE statement_id = ?",
            (
                status,
                superseded_by,
                status,
                supersession_reason or SUPERSESSION_UNKNOWN,
                status,
                ts,
                ts,
                str(statement_id),
            ),
        )
        self._store.execute(
            "UPDATE memory2_facts SET assertion_status = ?,"
            " superseded_by = COALESCE(?, superseded_by),"
            " revoked_at_ms = CASE WHEN ? = 'revoked' THEN ? ELSE revoked_at_ms END,"
            " updated_ms = ? WHERE fact_id = ?",
            (status, superseded_by, status, ts, ts, str(statement_id)),
        )

    def _redact(self, statement_id: str, *, ts: int) -> None:
        """Drop payload of a statement while keeping a content-free tombstone."""
        self._set_status(statement_id, status="revoked", ts=ts)
        self._store.execute(
            "DELETE FROM memory2_fact_principals WHERE fact_id = ?", (str(statement_id),)
        )
        self._store.execute(
            "DELETE FROM knowledge_statement_principals WHERE statement_id = ?",
            (str(statement_id),),
        )
        self._store.execute(
            "UPDATE memory2_nodes SET content = '', content_norm = '', updated_at = ?"
            " WHERE id = ?",
            (_iso(ts), str(statement_id)),
        )
        self._store.execute(
            "DELETE FROM memory2_nodes_fts WHERE entry_id = ?", (str(statement_id),)
        )
        self._store.execute(
            "DELETE FROM memory2_embeddings WHERE entry_id = ?", (str(statement_id),)
        )

    # ── jobs ─────────────────────────────────────────────────────────────────

    def enqueue_job(
        self,
        sources: tuple[SourceRef, ...],
        *,
        context: TrustedCaptureContext,
        extractor_version: str,
        scope_key: str,
        kind: str = "statement_extraction",
        due_ms: int | None = None,
        max_waiting: int | None = None,
        ts_ms: int | None = None,
    ) -> CaptureJobReceipt:
        self._policy.require_capture(context)
        if not sources:
            raise ValidationError("a capture job needs at least one source")
        for source in sources:
            if not self._authority.verify_source(source):
                raise KnowledgeError("denied_unknown_basis", "unknown job source")
            issued = self._authority.verify_source_ref(source.event_id, source.revision)
            if issued is None or issued != source:
                raise KnowledgeError("denied_unknown_basis", "job source provenance mismatch")
        job_id = _stable_id(
            "job",
            "|".join(f"{item.event_id}@{item.revision}" for item in sorted(sources, key=lambda s: s.key))
            + f"|{extractor_version}|{kind}",
        )
        ts = int(ts_ms) if ts_ms is not None else now_ms()
        existing = self._store.query_one(
            "SELECT state FROM knowledge_jobs WHERE job_id = ?", (job_id,)
        )
        if existing is not None and str(existing["state"]) in ("queued", "running", "done"):
            # ``already_queued`` lets a bounded historic pass skip this batch and keep
            # walking instead of stopping on work that is already in the queue.
            return CaptureJobReceipt(
                job_id=job_id, state=str(existing["state"]), reason="already_queued"
            )
        state, reason = "queued", None
        if max_waiting is not None and self.pending_job_count() >= max(1, int(max_waiting)):
            # Overflow is visible and recoverable: the job is recorded as ``skipped`` in
            # the same table, and a later replay may still queue it.  The sources stay
            # durable either way - a full queue never discards an observation.
            state, reason = "skipped", "queue_full"
        self._store.execute(
            """
            INSERT INTO knowledge_jobs (job_id, workspace_id, scope_key, kind, sources_json,
                extractor_version, state, reason, attempts, due_ms, created_ms, updated_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                state = excluded.state, reason = excluded.reason,
                due_ms = excluded.due_ms, updated_ms = excluded.updated_ms
            """,
            (
                job_id,
                self.workspace_id,
                scope_key,
                kind,
                json.dumps([{"event_id": s.event_id, "revision": s.revision} for s in sources]),
                extractor_version,
                state,
                reason,
                int(due_ms if due_ms is not None else ts),
                ts,
                ts,
            ),
        )
        return CaptureJobReceipt(job_id=job_id, state=state, reason=str(reason or ""))

    def job_record(self, job_id: str) -> CaptureJobRecord:
        """Internal worker read: one job with its resolvable sources."""
        row = self._store.query_one("SELECT * FROM knowledge_jobs WHERE job_id = ?", (str(job_id),))
        if row is None:
            raise KnowledgeError("unresolved", "unknown capture job")
        sources: list[SourceRef] = []
        unresolved: list[str] = []
        for event_id, revision in _job_source_pairs(row):
            issued = self._authority.verify_source_ref(event_id, revision)
            if issued is None:
                unresolved.append(f"{event_id}@{revision}")
                continue
            sources.append(issued)
        return CaptureJobRecord(
            job_id=str(row["job_id"]),
            state=str(row["state"]),
            reason=str(row["reason"] or ""),
            scope_key=str(row["scope_key"]),
            kind=str(row["kind"]),
            extractor_version=str(row["extractor_version"]),
            attempts=int(row["attempts"] or 0),
            due_ms=int(row["due_ms"] or 0),
            updated_ms=int(row["updated_ms"] or 0),
            sources=tuple(sources),
            unresolved=tuple(unresolved),
        )

    def stale_jobs(self, *, updated_before_ms: int, limit: int = 50) -> tuple[CaptureJobReceipt, ...]:
        """Jobs a crash left ``running``; the worker recovers them instead of stalling."""
        rows = self._store.query(
            "SELECT job_id, state, reason FROM knowledge_jobs"
            " WHERE state = 'running' AND updated_ms <= ?"
            " ORDER BY updated_ms, job_id LIMIT ?",
            (int(updated_before_ms), int(limit)),
        )
        return tuple(
            CaptureJobReceipt(
                job_id=str(row["job_id"]),
                state=str(row["state"]),
                reason=str(row["reason"] or ""),
            )
            for row in rows
        )

    def requeue_job(
        self, job_id: str, *, due_ms: int, reason: str = "", ts_ms: int | None = None
    ) -> CaptureJobReceipt:
        """Put a job back in the queue without losing its attempt counter."""
        ts = int(ts_ms) if ts_ms is not None else now_ms()
        self._store.execute(
            "UPDATE knowledge_jobs SET state = 'queued', reason = ?, due_ms = ?,"
            " updated_ms = ? WHERE job_id = ?",
            (str(reason or ""), int(due_ms), ts, str(job_id)),
        )
        return self.job_state(job_id)

    def status(self, *, now_ms: int) -> dict[str, Any]:
        """Read-only capture counters: states, refusal reasons and the oldest wait."""
        return capture_status(self._store, now_ms=now_ms)

    def job_state(self, job_id: str) -> CaptureJobReceipt:
        row = self._store.query_one("SELECT * FROM knowledge_jobs WHERE job_id = ?", (str(job_id),))
        if row is None:
            raise KnowledgeError("unresolved", "unknown capture job")
        return CaptureJobReceipt(
            job_id=str(row["job_id"]),
            state=str(row["state"]),
            reason=str(row["reason"] or ""),
        )

    def set_job_state(
        self, job_id: str, state: str, *, reason: str = "", ts_ms: int | None = None
    ) -> CaptureJobReceipt:
        ts = int(ts_ms) if ts_ms is not None else now_ms()
        self._store.execute(
            "UPDATE knowledge_jobs SET state = ?, reason = ?, updated_ms = ?,"
            " attempts = attempts + 1 WHERE job_id = ?",
            (str(state), str(reason or ""), ts, str(job_id)),
        )
        return self.job_state(job_id)

    def cancel_jobs_for_event(
        self, event_id: str, *, revision: int | None = None, ts: int | None = None
    ) -> tuple[str, ...]:
        """Cancel pending extraction jobs that referenced a now-invalid source."""
        rows = self._store.query(
            "SELECT job_id, sources_json, state FROM knowledge_jobs"
            " WHERE state IN ('queued','running')"
        )
        cancelled: list[str] = []
        for row in rows:
            try:
                refs = json.loads(str(row["sources_json"] or "[]"))
            except json.JSONDecodeError:  # pragma: no cover - defensive
                continue
            for ref in refs:
                if str(ref.get("event_id")) != str(event_id):
                    continue
                if revision is not None and int(ref.get("revision", -1)) != int(revision):
                    continue
                self._store.execute(
                    "UPDATE knowledge_jobs SET state = 'cancelled', reason = 'source_revoked',"
                    " updated_ms = ? WHERE job_id = ?",
                    (int(ts if ts is not None else now_ms()), str(row["job_id"])),
                )
                cancelled.append(str(row["job_id"]))
                break
        return tuple(cancelled)

    def pending_job_count(self) -> int:
        return int(
            self._store.scalar(
                "SELECT COUNT(*) FROM knowledge_jobs WHERE state IN ('queued','running')"
            )
            or 0
        )

    def due_jobs(self, *, now: int | None = None, limit: int = 20) -> tuple[CaptureJobReceipt, ...]:
        rows = self._store.query(
            "SELECT job_id, state, reason FROM knowledge_jobs WHERE state = 'queued'"
            " AND due_ms <= ? ORDER BY due_ms, job_id LIMIT ?",
            (int(now if now is not None else now_ms()), int(limit)),
        )
        return tuple(
            CaptureJobReceipt(
                job_id=str(row["job_id"]),
                state=str(row["state"]),
                reason=str(row["reason"] or ""),
            )
            for row in rows
        )

    def list_statements(
        self, *, cursor: str | None, limit: int, statuses: Iterable[str] | None = None
    ) -> StatementPage:
        ids, next_cursor = self.list_statement_ids(
            statuses=statuses, cursor=cursor, limit=limit
        )
        return StatementPage(
            items=tuple(self.summary(item) for item in ids),
            next_cursor=next_cursor,
        )

    def stats(self) -> dict[str, int]:
        return {
            "people_count": int(self._store.scalar("SELECT COUNT(*) FROM contacts") or 0),
            "statement_count": int(
                self._store.scalar("SELECT COUNT(*) FROM knowledge_statements") or 0
            ),
            "active_statement_count": int(
                self._store.scalar(
                    "SELECT COUNT(*) FROM knowledge_statements"
                    " WHERE status IN ('assertion','confirmed')"
                )
                or 0
            ),
            "quarantined_count": int(
                self._store.scalar("SELECT COUNT(*) FROM knowledge_quarantine") or 0
            ),
            "pending_jobs": self.pending_job_count(),
            "identity_revision": self._store.identity_revision,
            "acl_epoch": self._store.acl_epoch,
            "schema_version": self._store.schema_version,
        }

    def expire_due(self, *, now: int | None = None, limit: int = 500) -> int:
        """Mark statements expired once their validity window passed."""
        moment = int(now if now is not None else now_ms())
        rows = self._store.query(
            "SELECT statement_id FROM knowledge_statements WHERE valid_until_ms IS NOT NULL"
            " AND valid_until_ms <= ? AND status IN ('assertion','confirmed') LIMIT ?",
            (moment, int(limit)),
        )
        for row in rows:
            self._set_status(str(row["statement_id"]), status="expired", ts=moment)
        if rows:
            self._store.bump_acl_epoch()
        return len(rows)


def token_re():
    """Token pattern shared with the scoped legacy recall."""
    import re

    return re.compile(r"[0-9A-Za-z_]{2,}")


def _scope_key(source: SourceRef) -> str:
    return f"channel:{source.channel}:chat:{source.chat_id}"


def _content_hash(statement_id: str, content: str) -> str:
    return hashlib.sha256(f"{statement_id}\x00{content}".encode("utf-8")).hexdigest()


@dataclass(slots=True)
class StatementRescreenReport:
    """Outcome of applying the current screens to already-published statements."""

    checked: int = 0
    kept: int = 0
    superseded: tuple[str, ...] = ()
    reasons: dict[str, int] = field(default_factory=dict)
    dry_run: bool = True

    def as_lines(self) -> list[str]:
        mode = "would hide" if self.dry_run else "hid"
        lines = [
            f"checked {self.checked} statement(s); {mode} {len(self.superseded)};"
            f" kept {self.kept}"
        ]
        for reason, count in sorted(self.reasons.items()):
            lines.append(f"  {reason}: {count}")
        return lines


#: Bumped whenever a candidate rule changes, so a stored rejection names the rule set
#: that produced it.  A re-screen never revives an earlier rejection: only a fresh,
#: re-authorized capture can do that.
SCREEN_RULE_VERSION: Final[str] = "screen/2"


def rescreen_statements(
    store: KnowledgeStore,
    *,
    apply: bool = False,
    limit: int = 5000,
    now_ms: int | None = None,
    rule_version: str = SCREEN_RULE_VERSION,
) -> StatementRescreenReport:
    """Apply the current deterministic screens to stored statements.

    Tightening a screen has to be able to clean up after itself, otherwise a rule added
    today only ever applies to new candidates.  A refused statement is set to
    ``superseded`` with no replacement: it stops being readable, its text stays
    inspectable, and its source revision is untouched, so the observation and its
    authority record survive.  ``superseded`` is chosen over ``revoked`` on purpose -
    revocation is the source-revocation signal and must not be reused as a quality mark.
    """
    from yeoman_gateway.knowledge._memory.extraction_jobs import screen_statement_content

    timestamp = int(now_ms if now_ms is not None else store.now_ms())
    report = StatementRescreenReport(dry_run=not apply)
    rows = store.query(
        "SELECT s.statement_id, s.status, n.content FROM knowledge_statements s"
        " JOIN memory2_nodes n ON n.id = s.statement_id"
        " WHERE s.status IN ('assertion','confirmed')"
        " ORDER BY s.created_ms, s.statement_id LIMIT ?",
        (max(1, int(limit)),),
    )
    for row in rows:
        report.checked += 1
        content = str(row["content"] or "")
        verdict = screen_statement_content(content)
        if verdict.accepted:
            report.kept += 1
            continue
        reason = str(verdict.reason)
        report.reasons[reason] = report.reasons.get(reason, 0) + 1
        if not apply:
            report.superseded += (str(row["statement_id"]),)
            continue
        statement_id = str(row["statement_id"])
        # One short transaction per statement: the live capture worker writes to the same
        # database, and a single long transaction over every refused row would hold the
        # write lock until it times the other writer out.
        with store.transaction():
            # Status, reason and audit land in one transaction.  Only currently active
            # rows are touched: an already rejected row is never re-rejected and never
            # reactivated, and text, sources and audience stay exactly as they were.
            cursor = store.execute(
                "UPDATE knowledge_statements SET status = 'superseded',"
                " supersession_reason = ?, revision = revision + 1, updated_ms = ?"
                " WHERE statement_id = ? AND status IN ('assertion','confirmed')",
                (SUPERSESSION_QUALITY_REJECTED, timestamp, statement_id),
            )
            if not cursor.rowcount:
                continue
            store.execute(
                "UPDATE memory2_facts SET assertion_status = 'superseded', updated_ms = ?"
                " WHERE fact_id = ?",
                (timestamp, statement_id),
            )
            store.execute(
                "INSERT INTO knowledge_statement_audit (statement_id, operation,"
                " actor_principal, evidence_ref, reason, detail_json, created_ms)"
                " VALUES (?, 'rescreen', '', ?, ?, ?, ?)",
                (
                    statement_id,
                    str(rule_version),
                    f"screen:{reason}",
                    json.dumps(
                        {"reason": reason, "rule_version": str(rule_version)},
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    timestamp,
                ),
            )
        report.superseded += (statement_id,)
    return report


def capture_status(store: KnowledgeStore, *, now_ms: int) -> dict[str, Any]:
    """Read-only promotion counters.  Never contains statement content."""
    states: dict[str, int] = {}
    for row in store.query("SELECT state, COUNT(*) AS n FROM knowledge_jobs GROUP BY state"):
        states[str(row["state"])] = int(row["n"])
    reasons: dict[str, int] = {}
    for row in store.query(
        "SELECT reason, COUNT(*) AS n FROM knowledge_jobs"
        " WHERE reason IS NOT NULL AND reason <> '' GROUP BY reason"
    ):
        reasons[str(row["reason"])] = int(row["n"])
    oldest = store.query_one(
        "SELECT MIN(due_ms) AS oldest FROM knowledge_jobs WHERE state = 'queued'"
    )
    oldest_due = (
        int(oldest["oldest"]) if oldest is not None and oldest["oldest"] is not None else 0
    )
    return {
        "states": states,
        "reasons": reasons,
        "oldest_queued_ms": oldest_due,
        "oldest_queued_age_ms": max(0, int(now_ms) - oldest_due) if oldest_due else 0,
    }


def _job_source_pairs(row: Any) -> tuple[tuple[str, int], ...]:
    """The source keys a job row stores, as plain pairs.

    A job only ever stores ``(event_id, revision)``.  The provenance - channel, chat,
    author and event time - is re-read from the proof owner on every run, so a job can
    never carry a source identity the authority has since changed or withdrawn.
    """
    try:
        stored = json.loads(str(row["sources_json"] or "[]"))
    except (json.JSONDecodeError, TypeError, ValueError):  # pragma: no cover - defensive
        return ()
    pairs: list[tuple[str, int]] = []
    for item in stored:
        if not isinstance(item, dict):
            continue
        event_id = str(item.get("event_id") or "")
        if not event_id:
            continue
        pairs.append((event_id, max(1, int(item.get("revision") or 1))))
    return tuple(pairs)


def _dedupe_key(
    *,
    scope_key: str,
    source: SourceRef,
    content: str,
    extractor_version: str,
    audience: frozenset[str],
    visibility: str,
) -> str:
    """Source-bound identity of a statement.

    The same text from two independent sources or two audiences is *not* the same
    statement.  A replay of the same source revision with the same extractor is.
    """
    payload = "\x1f".join(
        (
            scope_key,
            source.event_id,
            str(int(source.revision)),
            " ".join(content.split()).strip().lower(),
            str(extractor_version),
            visibility,
            ",".join(sorted(audience)),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _stable_id(prefix: str, key: str) -> str:
    digest = hashlib.sha256(f"{prefix}:{key}".encode("utf-8")).hexdigest()
    return (
        f"{digest[0:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"
    )


def _iso(ms: int) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ms / 1000.0, tz=UTC).isoformat(timespec="seconds")


def _encode_cursor(params: list[Any], offset: int) -> str:
    payload = json.dumps({"p": params, "o": int(offset)}, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16] + ":" + str(int(offset))


def _decode_cursor(cursor: str, params: list[Any]) -> int:
    try:
        _prefix, _, offset = str(cursor).partition(":")
        parsed = int(offset)
    except (TypeError, ValueError):
        raise ValidationError("malformed cursor") from None
    payload = json.dumps({"p": params, "o": parsed}, separators=(",", ":"))
    if not str(cursor).startswith(hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]):
        raise ValidationError("cursor does not belong to this filter")
    return parsed


def validate_unresolved_mentions(mentions: Iterable[str]) -> tuple[str, ...]:
    return tuple(validate_name(item, "unresolved mention") for item in mentions)
