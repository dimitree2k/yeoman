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

    def _gate_clause(self, context: TrustedReadContext, decision: ReadDecision) -> tuple[str, list[Any]]:
        """SQL candidate filter: status, validity, revocation, source chat, audience.

        The statement must belong to the requested chat scope and *every* verified
        recipient must be named in its audience.  Role edges never widen this: a
        participant is not a reader.
        """
        recipients = self._acl_principals(decision)
        clauses = [
            "s.workspace_id = ?",
            "s.scope_key = ?",
            "s.status IN ('assertion','confirmed')",
            "s.revoked_at_ms IS NULL",
            "s.superseded_by IS NULL",
            "(s.valid_until_ms IS NULL OR s.valid_until_ms > ?)",
            "(s.valid_from_ms <= ?)",
        ]
        params: list[Any] = [
            self.workspace_id,
            context.scope_key(),
            int(context.now_ms),
            int(context.now_ms),
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
        clause = (
            "EXISTS (SELECT 1 FROM knowledge_statement_people kp"
            f" WHERE kp.statement_id = s.statement_id AND kp.person_id IN ({placeholders})"
            f"{role_sql})"
        )
        return clause, params

    def candidates(
        self, query: RecallQuery, *, context: TrustedReadContext
    ) -> tuple[CandidateRows, ReadDecision]:
        decision = self.decide(context)
        if not decision.allowed:
            return CandidateRows((), 0), decision
        clauses, params = self._gate_clause(context, decision)
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
        denied = self._denied_count(context, decision, query)
        if not statement_ids:
            return CandidateRows((), denied), decision

        ranked = self._rank(query.text, statement_ids)
        return CandidateRows(tuple(ranked[: query.limit]), denied), decision

    def _denied_count(
        self, context: TrustedReadContext, decision: ReadDecision, query: RecallQuery
    ) -> int:
        """How many statements exist in scope but are not readable.  Never exposed to users."""
        if not context.owner:
            return 0
        clauses, params = self._person_filter_clause(query.person_ids, query.roles)
        sql = (
            "SELECT COUNT(*) FROM knowledge_statements s"
            " WHERE s.workspace_id = ? AND s.scope_key = ? AND s.revoked_at_ms IS NULL"
        )
        bound: list[Any] = [self.workspace_id, context.scope_key()]
        if clauses:
            sql += f" AND {clauses}"
            bound.extend(params)
        total = int(self._store.scalar(sql, tuple(bound)) or 0)
        permitted = int(
            self._store.scalar(
                f"SELECT COUNT(*) FROM knowledge_statements s WHERE "
                f"{self._gate_clause(context, decision)[0]}",
                tuple(self._gate_clause(context, decision)[1]),
            )
            or 0
        )
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
                lowered = text.lower()
                for row in rows:
                    content = str(row["content"] or "").lower()
                    overlap = sum(1 for token in tokens if token in content)
                    scores[str(row["id"])] = overlap / max(1, len(tokens))
        ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        return [item for item, _score in ordered]

    # ── public read operations ───────────────────────────────────────────────

    def recall(self, query: RecallQuery, *, context: TrustedReadContext) -> KnowledgeContext:
        rows, decision = self.candidates(query, context=context)
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
        allowed_ids = self._recheck_ids(rows.statement_ids, context, decision)
        text, source_refs = self._render(allowed_ids, context, decision)
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

    def profile(self, person_id: str, *, context: TrustedReadContext) -> PersonProfile:
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
        result = self.recall(RecallQuery(person_ids=(canonical,), limit=20), context=context)
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
        self, statement_ids: tuple[str, ...], context: TrustedReadContext, decision: ReadDecision
    ) -> tuple[str, ...]:
        """Final id recheck with *current* rows, immediately before rendering."""
        if not decision.allowed or not statement_ids:
            return ()
        clauses, params = self._gate_clause(context, decision)
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
    ) -> tuple[str, tuple[SourceRef, ...]]:
        """Render permitted statements with eligible names.  Evidence ids stay structured."""
        if not statement_ids:
            return "", ()
        placeholders = ",".join("?" for _ in statement_ids)
        rows = self._store.query(
            "SELECT s.statement_id, s.status, n.content FROM knowledge_statements s"
            " JOIN memory2_nodes n ON n.id = s.statement_id"
            f" WHERE s.statement_id IN ({placeholders})",
            tuple(statement_ids),
        )
        contents = {str(row["statement_id"]): str(row["content"] or "") for row in rows}
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
