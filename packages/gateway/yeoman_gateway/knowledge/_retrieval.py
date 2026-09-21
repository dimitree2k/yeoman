"""Private retrieval engine: the one read gate for person knowledge.

Order of a read (never reordered):

1. re-validate the trusted read context against Policy and current chat membership,
2. gate statement status, validity, revocation, source chat and audience in SQL,
3. resolve person filters through original ids plus active redirects,
4. rank only the permitted candidates,
5. attach sources/revisions,
6. re-check right before the text is handed out.

Forbidden text never reaches a provider or an embedding boundary: the candidate set is
filtered first.  A denied read returns an empty context with a reason code, never a
partially redacted prompt.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Any

from yeoman_gateway.knowledge import _reasons
from yeoman_gateway.knowledge._identity import IdentityEngine
from yeoman_gateway.knowledge._statements import StatementEngine
from yeoman_gateway.knowledge._store import KnowledgeStore
from yeoman_gateway.knowledge.models import (
    KnowledgeContext,
    KnowledgeError,
    PersonProfile,
    PersonResolution,
    RecallQuery,
    SourceRef,
    TrustedReadContext,
    ValidationError,
)

MAX_CONTEXT_CHARS = 4000

#: The four reasons live in :mod:`yeoman_gateway.knowledge._reasons` so the writers and
#: the readers share one vocabulary without an import cycle.
SUPERSESSION_STATE_CHANGE: str = _reasons.SUPERSESSION_STATE_CHANGE
SUPERSESSION_CORRECTION: str = _reasons.SUPERSESSION_CORRECTION
SUPERSESSION_QUALITY_REJECTED: str = _reasons.SUPERSESSION_QUALITY_REJECTED
SUPERSESSION_UNKNOWN: str = _reasons.SUPERSESSION_UNKNOWN

#: The one read contract.  Every public reader resolves to exactly one of these views,
#: so profile, recall, roster, FTS/hybrid and model context can never disagree about a
#: status.  See the design table in §7.5.
STATEMENT_VIEWS: tuple[str, ...] = (
    "current",
    "historic",
    "correction_audit",
    "diagnosis",
)

#: A single-row predicate: true exactly when a statement contributes to a *current*
#: value.  Used as the final post-filter on rows a reader already selected.
CURRENT_STATEMENT_SQL: str = (
    "s.status IN ('assertion','confirmed','expired')"
    " AND s.revoked_at_ms IS NULL"
    " AND s.superseded_by IS NULL"
)

#: A single-row predicate for the historical view, bound to two parameters
#: (``at_ms``, ``at_ms``).  It asks one question - "does the proven period contain this
#: instant?" - and the period is half-open ``[start, end)``: reading exactly at its start
#: shows the value, reading at its end does not.  A period with an unknown start is never
#: a proof of the past, so ``valid_from_ms > 0`` is part of the condition.
HISTORIC_STATEMENT_SQL: str = (
    "s.status = 'superseded'"
    " AND s.superseded_by IS NULL"
    " AND s.supersession_reason = 'state_change'"
    " AND s.revoked_at_ms IS NULL"
    " AND s.valid_from_ms > 0"
    " AND s.valid_from_ms < ?"
    " AND (s.valid_until_ms IS NULL OR s.valid_until_ms > ?)"
)

#: ``revoked`` never appears, in any view: not even a diagnosis path lifts a source
#: revocation.  Everything else a diagnosis may see, because it is the audit surface.
DIAGNOSIS_STATEMENT_SQL: str = "s.status <> 'revoked' AND s.revoked_at_ms IS NULL"


def statement_visibility_clause(
    view: str, *, at_ms: int = 0
) -> tuple[str, tuple[Any, ...]]:
    """The shared status/reason predicate for one read view, plus its bound parameters.

    Returns a SQL fragment over the alias ``s`` and the parameters it needs, so a reader
    can splice it into its own candidate query instead of re-deriving the rules.
    """
    if view not in STATEMENT_VIEWS:
        raise ValidationError(f"unknown statement view: {view!r}")
    if view == "current":
        return CURRENT_STATEMENT_SQL, ()
    if view == "historic":
        return HISTORIC_STATEMENT_SQL, (int(at_ms), int(at_ms))
    if view == "correction_audit":
        return (
            "s.status = 'superseded' AND s.superseded_by IS NULL"
            " AND s.supersession_reason = 'correction'"
            " AND s.revoked_at_ms IS NULL",
            (),
        )
    return DIAGNOSIS_STATEMENT_SQL, ()


def row_is_visible(view: str, row: Any, *, at_ms: int = 0) -> bool:
    """Python twin of :func:`statement_visibility_clause`, for already-loaded rows.

    Both spellings exist on purpose: SQL reduces the candidate set, and this check
    decides the final answer on the row that is actually about to be handed out.
    """
    if view not in STATEMENT_VIEWS:
        raise ValidationError(f"unknown statement view: {view!r}")
    status = str(row["status"] or "")
    revoked = row["revoked_at_ms"] is not None
    superseded_by = row["superseded_by"] is not None
    if revoked or status == "revoked":
        return False
    if view == "diagnosis":
        return True
    if status in ("assertion", "confirmed", "expired"):
        return not superseded_by
    if status != "superseded" or superseded_by:
        return False
    reason = str(row["supersession_reason"] or SUPERSESSION_UNKNOWN)
    if view == "current":
        return False
    if view == "correction_audit":
        return reason == SUPERSESSION_CORRECTION
    # Historical: only a proven state change, and only inside its proven period.
    if reason != SUPERSESSION_STATE_CHANGE:
        return False
    start = int(row["valid_from_ms"] or 0)
    if start <= 0 or start >= int(at_ms):
        return False
    until = row["valid_until_ms"]
    return until is None or int(until) > int(at_ms)


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True, slots=True)
class ReadDecision:
    """Result of validating a read context before any content is touched."""

    allowed: bool
    reason: str
    members: frozenset[str] | None
    recipients: tuple[str, ...]
    membership_revision: str | None


@dataclass(frozen=True, slots=True)
class CandidateRows:
    statement_ids: tuple[str, ...]
    denied: int


class RetrievalEngine:
    """Recall and profile projection over permitted statements."""

    def __init__(
        self,
        store: KnowledgeStore,
        *,
        identity: IdentityEngine,
        statements: StatementEngine,
        policy: Any,
        workspace_id: str,
    ) -> None:
        self._store = store
        self._identity = identity
        self._statements = statements
        self._policy = policy
        self.workspace_id = str(workspace_id)

    # ── context validation ───────────────────────────────────────────────────

    def decide(self, context: TrustedReadContext) -> ReadDecision:
        """Validate the trusted context.  A failure here means *no* content at all."""
        if not isinstance(context, TrustedReadContext):
            raise ValidationError("read context must be a TrustedReadContext")
        revision = self._policy.current_policy_revision()
        if int(context.policy_revision) != int(revision):
            return ReadDecision(False, "stale_policy_revision", None, (), None)

        membership = self._policy.membership(context)
        members = None if membership is None else frozenset(membership.members)
        membership_revision = None if membership is None else str(membership.revision)

        if members is None:
            return ReadDecision(False, "membership_unknown", None, (), None)
        if context.principal_id not in members:
            return ReadDecision(False, "reader_not_a_member", members, (), membership_revision)

        if context.principal_id not in (context.recipient_principals or frozenset()):
            return ReadDecision(
                False, "reader_not_in_target_audience", members, (), membership_revision
            )

        recipients = tuple(sorted(context.recipient_principals or frozenset()))
        missing = sorted(set(recipients) - members)
        if missing:
            return ReadDecision(
                False, "recipient_not_a_member", members, recipients, membership_revision
            )
        if not recipients:
            return ReadDecision(False, "no_verified_recipient", members, (), membership_revision)
        return ReadDecision(True, "ok", members, recipients, membership_revision)

    def _acl_principals(self, decision: ReadDecision) -> tuple[str, ...]:
        """Every verified human recipient must be inside the statement audience."""
        return tuple(sorted(set(decision.recipients)))

    # ── candidate selection ──────────────────────────────────────────────────

    def _read_time_ms(self, context: TrustedReadContext, view: str) -> int:
        """The instant a view is evaluated at.

        A historical read is evaluated *at* the requested instant, so a statement whose
        proven period contains that instant stays visible even though it has since been
        replaced.  Every other view is evaluated now.
        """
        if view == "historic":
            return int(context.now_ms)
        return int(context.now_ms)

    def _gate_clause(
        self,
        context: TrustedReadContext,
        decision: ReadDecision,
        *,
        view: str = "current",
    ) -> tuple[str, list[Any]]:
        """SQL candidate filter: status, validity, revocation, source chat, audience.

        The statement must belong to the requested chat scope and *every* verified
        recipient must be named in its audience.  Role edges never widen this: a
        participant is not a reader.  The status/reason rules come from the one shared
        contract in :func:`statement_visibility_clause`; nothing here re-derives them.
        """
        recipients = self._acl_principals(decision)
        at_ms = self._read_time_ms(context, view)
        visibility, visibility_params = statement_visibility_clause(view, at_ms=at_ms)
        clauses = [
            "s.workspace_id = ?",
            "s.scope_key = ?",
            f"({visibility})",
            "(s.valid_until_ms IS NULL OR s.valid_until_ms > ?)",
            "(s.valid_from_ms <= ?)",
        ]
        params: list[Any] = [
            self.workspace_id,
            context.scope_key(),
            *visibility_params,
            at_ms,
            at_ms,
        ]
        # author_only means "only the author reads this" - never "nobody".  Status,
        # expiry and revocation were already checked above for every scope.
        clauses.append(
            "((s.visibility_scope = 'author_only' AND s.author_principal = ?)"
            " OR (s.visibility_scope <> 'author_only'))"
        )
        params.append(context.principal_id)
        # Every verified recipient must be named in the audience.  actor_only resolves
        # through its author principal, which the gate above read from the current row.
        placeholders = ",".join("?" for _ in recipients)
        audience_match = (
            "(SELECT COUNT(DISTINCT p.principal_id)"
            " FROM knowledge_statement_principals p"
            f" WHERE p.statement_id = s.statement_id AND p.role = 'audience'"
            f" AND p.principal_id IN ({placeholders})) = ?"
        )
        if recipients:
            clauses.append(
                "((s.visibility_scope <> 'author_only' AND " + audience_match + ")"
                " OR (s.visibility_scope = 'author_only' AND s.author_principal = ?))"
            )
            params.extend(recipients)
            params.append(len(recipients))
            params.append(context.principal_id)
        else:
            clauses.append(
                "((s.visibility_scope <> 'author_only' AND " + audience_match + ")"
                " OR (s.visibility_scope = 'author_only' AND 0))"
            )
            params.append("__no_recipient__")
            params.append(0)
        return " AND ".join(clauses), params

    def _person_filter_clause(
        self, person_ids: tuple[str, ...], roles: tuple[str, ...]
    ) -> tuple[str, list[Any]]:
        """Person filter through *original* ids plus active redirects."""
        if not person_ids:
            return "", []
        originals: set[str] = set()
        for person_id in person_ids:
            originals.add(str(person_id))
            row = self._store.query_one(
                "SELECT source_id FROM knowledge_identity_redirects"
                " WHERE target_id = ? AND active = 1",
                (str(person_id),),
            )
            if row is not None:
                originals.add(str(row["source_id"]))
        # A redirect chain may be longer than one hop.
        for _ in range(8):
            placeholders = ",".join("?" for _ in originals)
            rows = self._store.query(
                "SELECT source_id FROM knowledge_identity_redirects"
                f" WHERE target_id IN ({placeholders}) AND active = 1",
                tuple(sorted(originals)),
            )
            grown = {str(row["source_id"]) for row in rows} - originals
            if not grown:
                break
            originals |= grown
        values = sorted(originals)
        placeholders = ",".join("?" for _ in values)
        role_sql = ""
        params: list[Any] = list(values)
        if roles:
            role_placeholders = ",".join("?" for _ in roles)
            role_sql = f" AND kp.role IN ({role_placeholders})"
            params.extend(roles)
        # Only an active role links a statement to a person: a withheld cutover row must
        # not make a statement discoverable through a person filter.
        clause = (
            "EXISTS (SELECT 1 FROM knowledge_statement_people kp"
            f" WHERE kp.statement_id = s.statement_id AND kp.person_id IN ({placeholders})"
            " AND kp.status = 'active'"
            f"{role_sql})"
        )
        return clause, params

    def candidates(
        self, query: RecallQuery, *, context: TrustedReadContext, view: str = "current"
    ) -> tuple[CandidateRows, ReadDecision]:
        decision = self.decide(context)
        if not decision.allowed:
            return CandidateRows((), 0), decision
        clauses, params = self._gate_clause(context, decision, view=view)
        person_clause, person_params = self._person_filter_clause(query.person_ids, query.roles)
        if person_clause:
            clauses = f"{clauses} AND {person_clause}"
            params.extend(person_params)
        rows = self._store.query(
            f"SELECT s.statement_id, s.content_hash FROM knowledge_statements s"
            f" WHERE {clauses} ORDER BY s.created_ms DESC, s.statement_id DESC LIMIT ?",
            (*params, int(query.limit) * 8),
        )
        statement_ids = [str(row["statement_id"]) for row in rows]
        denied = self._denied_count(context, decision, query, clauses=clauses, params=params)
        if not statement_ids:
            return CandidateRows((), denied), decision

        ranked = self._rank(query.text, statement_ids)
        return CandidateRows(tuple(ranked[: query.limit]), denied), decision

    def _denied_count(
        self,
        context: TrustedReadContext,
        decision: ReadDecision,
        query: RecallQuery,
        *,
        clauses: str,
        params: list[Any],
    ) -> int:
        """Statements in scope that this reader may not see.

        Only an explicitly owner-authorized caller receives this number; a normal reader
        learns nothing about how much was withheld.
        """
        if not context.owner:
            return 0
        permitted = int(
            self._store.scalar(
                f"SELECT COUNT(*) FROM knowledge_statements s WHERE {clauses}",
                tuple(params),
            )
            or 0
        )
        person_clause, person_params = self._person_filter_clause(query.person_ids, query.roles)
        visibility, visibility_params = statement_visibility_clause("current")
        sql = (
            "SELECT COUNT(*) FROM knowledge_statements s"
            f" WHERE s.workspace_id = ? AND s.scope_key = ? AND ({visibility})"
            " AND (s.valid_until_ms IS NULL OR s.valid_until_ms > ?)"
        )
        bound: list[Any] = [
            self.workspace_id,
            context.scope_key(),
            *visibility_params,
            int(context.now_ms),
        ]
        if person_clause:
            sql += f" AND {person_clause}"
            bound.extend(person_params)
        total = int(self._store.scalar(sql, tuple(bound)) or 0)
        return max(0, total - permitted)

    def _rank(self, text: str, statement_ids: list[str]) -> list[str]:
        """Rank permitted candidates only.  Never touch a row that failed the gate."""
        if not statement_ids:
            return []
        if not text.strip():
            return list(statement_ids)
        placeholders = ",".join("?" for _ in statement_ids)
        tokens = re.findall(r"[0-9A-Za-z_]{2,}", text.lower())[:16]
        scores: dict[str, float] = {item: 0.0 for item in statement_ids}
        if tokens:
            match_query = " OR ".join(tokens)
            try:
                rows = self._store.query(
                    "SELECT n.id AS id, bm25(memory2_nodes_fts) AS score"
                    " FROM memory2_nodes_fts"
                    " JOIN memory2_nodes n ON n.id = memory2_nodes_fts.entry_id"
                    f" WHERE memory2_nodes_fts MATCH ? AND n.id IN ({placeholders})",
                    (match_query, *statement_ids),
                )
                for row in rows:
                    raw = float(row["score"] if row["score"] is not None else 0.0)
                    scores[str(row["id"])] = 1.0 / (1.0 + max(0.0, raw))
            except Exception:
                rows = self._store.query(
                    "SELECT n.id AS id, n.content_norm AS content FROM memory2_nodes n"
                    f" WHERE n.id IN ({placeholders})",
                    tuple(statement_ids),
                )
                for row in rows:
                    content = str(row["content"] or "").lower()
                    overlap = sum(1 for token in tokens if token in content)
                    scores[str(row["id"])] = overlap / max(1, len(tokens))
        ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        return [item for item, _score in ordered]

    # ── public read operations ───────────────────────────────────────────────

    def recall(
        self, query: RecallQuery, *, context: TrustedReadContext, view: str = "current"
    ) -> KnowledgeContext:
        rows, decision = self.candidates(query, context=context, view=view)
        if not decision.allowed:
            return KnowledgeContext(
                text="",
                statement_ids=(),
                source_refs=(),
                identity_revision=self._store.identity_revision,
                acl_epoch=self._store.acl_epoch,
                context_revision=self.context_revision(context, decision, ()),
                reason=decision.reason,
            )
        allowed_ids = self._recheck_ids(rows.statement_ids, context, decision, view=view)
        text, source_refs = self._render(allowed_ids, context, decision, view=view)
        revision = self.context_revision(context, decision, allowed_ids)
        return KnowledgeContext(
            text=text,
            statement_ids=allowed_ids,
            source_refs=source_refs,
            identity_revision=self._store.identity_revision,
            acl_epoch=self._store.acl_epoch,
            context_revision=revision,
            reason="ok" if allowed_ids else "empty",
            denied_count=rows.denied,
        )

    def recall_hybrid(
        self,
        query: RecallQuery,
        *,
        context: TrustedReadContext,
        embedder: Any | None = None,
        preprocessing_version: str | None = None,
    ) -> KnowledgeContext:
        """Merge exact/FTS and vector candidates by statement identity, then gate again.

        The lexical path is the base: it is produced first and never depends on a
        provider.  Vector candidates only ever *add* ids, are filtered by the same SQL
        gate, and are dropped silently when the provider fails - an outage costs recall,
        never a lexically findable statement.  Immediately before rendering, every merged
        id is re-checked against current rights and revocation.
        """
        decision = self.decide(context)
        if not decision.allowed:
            return KnowledgeContext(
                text="",
                statement_ids=(),
                source_refs=(),
                identity_revision=self._store.identity_revision,
                acl_epoch=self._store.acl_epoch,
                context_revision=self.context_revision(context, decision, ()),
                reason=decision.reason,
            )
        rows, decision = self.candidates(query, context=context)
        merged = list(rows.statement_ids)
        for statement_id in self._vector_candidates(
            query,
            context=context,
            embedder=embedder,
            preprocessing_version=preprocessing_version,
        ):
            if statement_id not in merged:
                merged.append(statement_id)
        allowed_ids = self._recheck_ids(tuple(merged), context, decision)
        text, source_refs = self._render(allowed_ids, context, decision)
        return KnowledgeContext(
            text=text,
            statement_ids=allowed_ids,
            source_refs=source_refs,
            identity_revision=self._store.identity_revision,
            acl_epoch=self._store.acl_epoch,
            context_revision=self.context_revision(context, decision, allowed_ids),
            reason="ok" if allowed_ids else "empty",
            denied_count=rows.denied,
        )

    def _vector_candidates(
        self,
        query: RecallQuery,
        *,
        context: TrustedReadContext,
        embedder: Any | None,
        preprocessing_version: str | None = None,
    ) -> tuple[str, ...]:
        """Statement ids whose embedding section is nearest, or nothing at all.

        The lookup goes through the store's own guarded search, which binds the dimension
        to the query vector's length and the model and preprocessing version to the
        caller's, and re-checks provenance and revocation immediately before the row is
        returned.  Every failure mode - no provider, no index, a provider error, a version
        mismatch, a retired row - resolves to "no extra candidates" rather than an error,
        which is what keeps FTS usable when embeddings are not.
        """
        if embedder is None or not query.text.strip():
            return ()
        if not self._store.has_table("memory2_embedding_index"):
            return ()
        try:
            vector = embedder.embed(query.text)
        except Exception:
            return ()
        if not vector:
            return ()
        from yeoman_gateway.knowledge._memory.embeddings import (
            EMBEDDING_PREPROCESSING_VERSION,
        )
        from yeoman_gateway.knowledge._memory.store import MemoryStore

        memory = MemoryStore(owner=self._store)
        rows = memory.search_embedding_index(
            workspace_id=self.workspace_id,
            query_vector=list(vector),
            scope_keys=[context.scope_key()],
            limit=64,
            model_id=str(getattr(embedder, "model", "") or "") or None,
            preprocessing_version=preprocessing_version or EMBEDDING_PREPROCESSING_VERSION,
        )
        return tuple(str(row["node_id"]) for row in rows)

    def profile(
        self, person_id: str, *, context: TrustedReadContext, view: str = "current"
    ) -> PersonProfile:
        """Profile projection: person plus the statements this reader may see."""
        decision = self.decide(context)
        canonical = self._identity.canonical_id(person_id)
        person = self._identity.get_person(canonical)
        resolution = PersonResolution(
            status="resolved" if person is not None else "unresolved",
            person_id=None if person is None else canonical,
            display_name=(
                None
                if person is None
                else self._identity.display_name(
                    canonical, context=context, for_group=not context.is_direct
                )
            ),
            identity_revision=self._store.identity_revision,
            reason="profile" if person is not None else "unknown_person",
        )
        if person is None or not decision.allowed:
            return PersonProfile(
                person=resolution,
                context=KnowledgeContext(
                    reason=decision.reason if not decision.allowed else "empty",
                    identity_revision=self._store.identity_revision,
                    acl_epoch=self._store.acl_epoch,
                ),
            )
        result = self.recall(
            RecallQuery(person_ids=(canonical,), limit=20), context=context, view=view
        )
        return PersonProfile(person=resolution, context=result)

    def revalidate(
        self, result: KnowledgeContext, *, context: TrustedReadContext
    ) -> KnowledgeContext:
        """Re-check a prepared context against current rights, membership and sources.

        Returns a context with only the currently permitted ids/text.  A caller that
        sees an empty result must discard the whole draft - filtering single sentences
        out of an already generated answer is not authorization.
        """
        decision = self.decide(context)
        if not decision.allowed:
            return KnowledgeContext(
                text="",
                statement_ids=(),
                source_refs=(),
                identity_revision=self._store.identity_revision,
                acl_epoch=self._store.acl_epoch,
                context_revision=self.context_revision(context, decision, ()),
                reason=decision.reason,
            )
        if result.acl_epoch != self._store.acl_epoch:
            pass  # epoch changed: the id recheck below is the authoritative decision
        allowed_ids = self._recheck_ids(result.statement_ids, context, decision)
        if not allowed_ids:
            return KnowledgeContext(
                text="",
                statement_ids=(),
                source_refs=(),
                identity_revision=self._store.identity_revision,
                acl_epoch=self._store.acl_epoch,
                context_revision=self.context_revision(context, decision, ()),
                reason="stale_context",
            )
        text, source_refs = self._render(allowed_ids, context, decision)
        if result.context_revision and result.context_revision != self.context_revision(
            context, decision, allowed_ids
        ):
            # The context was prepared against different source revisions.
            return KnowledgeContext(
                text=text,
                statement_ids=allowed_ids,
                source_refs=source_refs,
                identity_revision=self._store.identity_revision,
                acl_epoch=self._store.acl_epoch,
                context_revision=self.context_revision(context, decision, allowed_ids),
                reason="revalidated",
            )
        return KnowledgeContext(
            text=text,
            statement_ids=allowed_ids,
            source_refs=source_refs,
            identity_revision=self._store.identity_revision,
            acl_epoch=self._store.acl_epoch,
            context_revision=self.context_revision(context, decision, allowed_ids),
            reason="ok",
        )

    def _recheck_ids(
        self,
        statement_ids: tuple[str, ...],
        context: TrustedReadContext,
        decision: ReadDecision,
        *,
        view: str = "current",
    ) -> tuple[str, ...]:
        """Final id recheck with *current* rows, immediately before rendering.

        This is where a revocation or a rights change that happened while the answer was
        being prepared wins over the already-selected candidates.
        """
        if not decision.allowed or not statement_ids:
            return ()
        clauses, params = self._gate_clause(context, decision, view=view)
        placeholders = ",".join("?" for _ in statement_ids)
        rows = self._store.query(
            f"SELECT s.statement_id FROM knowledge_statements s"
            f" WHERE {clauses} AND s.statement_id IN ({placeholders})",
            (*params, *statement_ids),
        )
        allowed = {str(row["statement_id"]) for row in rows}
        return tuple(item for item in statement_ids if item in allowed)

    def _render(
        self,
        statement_ids: tuple[str, ...],
        context: TrustedReadContext,
        decision: ReadDecision,
        *,
        view: str = "current",
    ) -> tuple[str, tuple[SourceRef, ...]]:
        """Render permitted statements with eligible names.  Evidence ids stay structured.

        Text is only ever read for ids that survived the gate *and* a fresh recheck, and
        only active person roles supply labels: a withheld role never names anybody.
        """
        if not statement_ids:
            return "", ()
        placeholders = ",".join("?" for _ in statement_ids)
        rows = self._store.query(
            "SELECT s.statement_id, s.status, s.superseded_by, s.supersession_reason,"
            " s.valid_from_ms, s.valid_until_ms, s.revoked_at_ms, n.content"
            " FROM knowledge_statements s"
            " JOIN memory2_nodes n ON n.id = s.statement_id"
            f" WHERE s.statement_id IN ({placeholders})",
            tuple(statement_ids),
        )
        at_ms = self._read_time_ms(context, view)
        contents = {
            str(row["statement_id"]): str(row["content"] or "")
            for row in rows
            if row_is_visible(view, row, at_ms=at_ms)
        }
        people = self._identity.person_ids_for_statements(statement_ids)
        names: dict[str, str | None] = {}
        lines: list[str] = []
        used: list[SourceRef] = []
        total = 0
        for statement_id in statement_ids:
            content = contents.get(statement_id, "")
            if not content:
                continue
            person_ids = people.get(statement_id, ())
            labels: list[str] = []
            for person_id in person_ids:
                if person_id not in names:
                    names[person_id] = self._identity.display_name(
                        person_id, context=context, for_group=not context.is_direct
                    )
                name = names[person_id] or "unbekannt"
                if name not in labels:
                    labels.append(name)
            line = content if not labels else f"{content} ({', '.join(labels)})"
            if total + len(line) > MAX_CONTEXT_CHARS:
                break
            total += len(line) + 1
            lines.append(line)
            for source, status in self._statements.sources_of(statement_id):
                if status == "active" and source not in used:
                    used.append(source)
        return "\n".join(lines), tuple(used)

    def context_revision(
        self, context: TrustedReadContext, decision: ReadDecision, statement_ids: tuple[str, ...]
    ) -> str:
        """A stable fingerprint of everything this context depends on."""
        source_rows: list[tuple[str, int]] = []
        if statement_ids:
            placeholders = ",".join("?" for _ in statement_ids)
            rows = self._store.query(
                "SELECT event_id, revision, status FROM knowledge_statement_sources"
                f" WHERE statement_id IN ({placeholders}) ORDER BY event_id, revision",
                tuple(statement_ids),
            )
            source_rows = [
                (str(row["event_id"]), int(row["revision"])) for row in rows
            ]
        payload = json.dumps(
            {
                "principal": context.principal_id,
                "scope": context.scope_key(),
                "recipients": sorted(decision.recipients),
                "membership": decision.membership_revision,
                "policy": int(context.policy_revision),
                "acl_epoch": self._store.acl_epoch,
                "statements": sorted(statement_ids),
                "sources": source_rows,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

    # ── authority helpers used by the composition root ───────────────────────

    def require_read(self, context: TrustedReadContext) -> ReadDecision:
        decision = self.decide(context)
        if not decision.allowed:
            raise KnowledgeError("unauthorized", decision.reason)
        return decision
