"""Public boundary of the person-knowledge module.

Runtime consumers (responder, tools, CLI, bootstrap, background jobs) import *only*
this module and :mod:`yeoman_gateway.knowledge.models`.  Everything else in the package
is private implementation.

``open_knowledge_store`` is the factory used by the composition root.  It validates the
schema, refuses to pretend that an empty store replaces existing legacy data, and never
runs a migration on the side.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable

from yeoman_gateway.knowledge._identity import IdentityEngine
from yeoman_gateway.knowledge._retrieval import RetrievalEngine
from yeoman_gateway.knowledge._statements import StatementEngine
from yeoman_gateway.knowledge._store import SCHEMA_VERSION, KnowledgeStore, StorageUnavailable
from yeoman_gateway.knowledge.authority import (
    PolicyAuthority,
    SourceAuthority,
    wall_clock_ms,
)
from yeoman_gateway.knowledge.models import (
    CaptureJobReceipt,
    CaptureResult,
    ChangeReceipt,
    EndpointResolution,
    Identifier,
    KnowledgeContext,
    KnowledgeError,
    KnowledgeStats,
    MaintenanceReport,
    PersonProfile,
    PersonResolution,
    RecallQuery,
    SourceRef,
    StatementCandidate,
    StatementPage,
    StatementSummary,
    TrustedAdminContext,
    TrustedCaptureContext,
    TrustedIdentityObservation,
    TrustedReadContext,
    ValidationError,
)

__all__ = [
    "KnowledgeService",
    "KnowledgeStartupError",
    "open_knowledge_store",
    "workspace_id_for",
]


class KnowledgeStartupError(KnowledgeError):
    """The knowledge store cannot be opened for the requested runtime state."""


def workspace_id_for(workspace: Path | str) -> str:
    return hashlib.sha1(str(Path(workspace).expanduser().resolve()).encode("utf-8")).hexdigest()[
        :16
    ]


def _has_legacy_schema(db_path: Path) -> bool:
    if not db_path.exists():
        return False
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:  # pragma: no cover - defensive
        return False
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    except sqlite3.Error:  # pragma: no cover - defensive
        return False
    finally:
        conn.close()
    names = {str(row[0]) for row in rows}
    return bool(names & {"memory2_nodes", "memory2_facts", "contact_identifiers"})


def open_knowledge_store(
    db_path: Path | str,
    *,
    workspace_id: str,
    source_authority: SourceAuthority,
    policy_authority: PolicyAuthority,
    clock: Any | None = None,
    create: bool = True,
    retention_ms: int | None = None,
    legacy_sources: Iterable[Path] = (),
) -> "KnowledgeService":
    """Open (or create) the knowledge store and return the public service.

    Raises :class:`KnowledgeStartupError` with ``migration_required`` when legacy data
    exists but no verified knowledge store was published, and ``schema_incompatible``
    when the file carries a different schema version.  Both are explicit states, never
    a silent fallback to an older store.
    """
    path = Path(db_path).expanduser()
    legacy = [Path(item).expanduser() for item in legacy_sources]
    fresh = not path.exists()
    if fresh and any(item.exists() for item in legacy):
        raise KnowledgeStartupError(
            "migration_required",
            "legacy memory/contacts data exists but no verified knowledge store was built",
        )
    try:
        store = KnowledgeStore(path, create=create)
    except sqlite3.Error as exc:  # pragma: no cover - defensive
        raise KnowledgeStartupError("storage_unavailable", str(exc)) from exc
    if fresh and create:
        # A brand-new installation with no legacy data is complete by construction.
        store.set_meta("migration_complete", "1")
        store.set_meta("migration_id", "fresh-install")
        store.commit_if_idle()
    version = store.schema_version
    if version and version != SCHEMA_VERSION:
        store.close()
        raise KnowledgeStartupError(
            "schema_incompatible",
            f"knowledge schema version {version} is not supported (need {SCHEMA_VERSION})",
        )
    if not fresh and not store.migration_complete():
        if _has_legacy_schema(path) or any(item.exists() for item in legacy):
            store.close()
            raise KnowledgeStartupError(
                "migration_required",
                "knowledge store exists but carries no complete migration manifest",
            )
    return KnowledgeService(
        store=store,
        workspace_id=workspace_id,
        source_authority=source_authority,
        policy_authority=policy_authority,
        clock=clock,
        retention_ms=retention_ms,
    )


def _scope_of(sources: tuple[SourceRef, ...] | list[SourceRef]) -> str:
    if not sources:
        return ""
    first = sources[0]
    return f"channel:{first.channel}:chat:{first.chat_id}"


class KnowledgeService:
    """The public facade.  One instance per gateway process, owned by bootstrap."""

    def __init__(
        self,
        *,
        store: KnowledgeStore,
        workspace_id: str,
        source_authority: SourceAuthority,
        policy_authority: PolicyAuthority,
        clock: Any | None = None,
        retention_ms: int | None = None,
    ) -> None:
        self._store = store
        self._authority = source_authority
        self._policy = policy_authority
        self.workspace_id = str(workspace_id)
        self._clock = clock
        self._identity = IdentityEngine(store, authority=source_authority, policy=policy_authority)
        self._statements = StatementEngine(
            store,
            identity=self._identity,
            authority=source_authority,
            policy=policy_authority,
            workspace_id=self.workspace_id,
            retention_ms=retention_ms,
        )
        self._retrieval = RetrievalEngine(
            store,
            identity=self._identity,
            statements=self._statements,
            policy=policy_authority,
            workspace_id=self.workspace_id,
        )

    # ── lifecycle ────────────────────────────────────────────────────────────

    @property
    def db_path(self) -> Path:
        return self._store.db_path

    def close(self) -> None:
        self._store.close()

    def __enter__(self) -> "KnowledgeService":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _now(self) -> int:
        if self._clock is not None:
            return int(self._clock.now_ms())
        return wall_clock_ms()

    def _read_context(self, context: TrustedReadContext) -> TrustedReadContext:
        """Server-side refresh of the trusted read context (never from a model)."""
        if not isinstance(context, TrustedReadContext):
            raise ValidationError("read context must be a TrustedReadContext")
        if context.now_ms <= 0:
            return replace(context, now_ms=self._now())
        return context

    # ── identity ─────────────────────────────────────────────────────────────

    def resolve_person(
        self,
        observation: TrustedIdentityObservation,
        *,
        context: TrustedReadContext | None = None,
    ) -> PersonResolution:
        """Resolve a verified platform observation.  Creates at most one stub."""
        if not isinstance(observation, TrustedIdentityObservation):
            raise ValidationError("observation must be a TrustedIdentityObservation")
        return self._identity.resolve_observation(
            observation, context=context, create_stub=True
        )

    def search_people(
        self, name: str, *, context: TrustedReadContext
    ) -> tuple[PersonResolution, ...]:
        """Search by name.  Several people may legitimately share a name."""
        checked = self._read_context(context)
        self._retrieval.require_read(checked)
        return self._identity.search_by_name(name, context=checked)

    def set_preferred_name(
        self,
        person_id: str,
        name: str,
        *,
        context: TrustedAdminContext,
        source: str = "owner_confirmed",
        visibility: str = "public",
        observed_name: str | None = None,
    ) -> ChangeReceipt:
        with self._store.transaction():
            receipt = self._identity.set_preferred_name(
                person_id,
                name,
                context=context,
                source=source,
                visibility=visibility,
                observed_name=observed_name,
            )
        return receipt

    def bind_identifier(
        self,
        person_id: str,
        identifier: Identifier,
        *,
        evidence_ref: str,
        mapping_verified: bool,
        context: TrustedAdminContext,
    ) -> ChangeReceipt:
        with self._store.transaction():
            return self._identity.add_binding(
                person_id=person_id,
                identifier=identifier,
                evidence_ref=evidence_ref,
                mapping_verified=mapping_verified,
                context=context,
            )

    def resolve_endpoint(
        self,
        person_id: str,
        channel: str,
        *,
        context: TrustedReadContext,
        prefer_kind: str | None = None,
    ) -> EndpointResolution:
        """A delivery endpoint needs an explicit kind when a person has several."""
        checked = self._read_context(context)
        self._retrieval.require_read(checked)
        return self._identity.resolve_endpoint(person_id, channel, prefer_kind=prefer_kind)

    # ── statements ───────────────────────────────────────────────────────────

    def capture(
        self, candidate: StatementCandidate, *, context: TrustedCaptureContext
    ) -> CaptureResult:
        """Validate and persist one statement with its person roles."""
        with self._store.transaction():
            return self._statements.capture(candidate, context=context)

    def recall(self, query: RecallQuery, *, context: TrustedReadContext) -> KnowledgeContext:
        checked = self._read_context(context)
        return self._retrieval.recall(query, context=checked)

    def profile(self, person_id: str, *, context: TrustedReadContext) -> PersonProfile:
        checked = self._read_context(context)
        return self._retrieval.profile(person_id, context=checked)

    def revalidate(
        self, result: KnowledgeContext, *, context: TrustedReadContext
    ) -> KnowledgeContext:
        checked = self._read_context(context)
        return self._retrieval.revalidate(result, context=checked)

    # ── identity changes ─────────────────────────────────────────────────────

    def merge_people(
        self,
        target_id: str,
        source_id: str,
        *,
        expected_revision: int,
        context: TrustedAdminContext,
    ) -> ChangeReceipt:
        with self._store.transaction():
            return self._identity.merge_people(
                target_id,
                source_id,
                expected_revision=expected_revision,
                context=context,
            )

    def undo_merge(
        self, operation_id: str, *, expected_revision: int, context: TrustedAdminContext
    ) -> ChangeReceipt:
        with self._store.transaction():
            return self._identity.undo_merge(
                operation_id, expected_revision=expected_revision, context=context
            )

    # ── statement lifecycle ──────────────────────────────────────────────────

    def invalidate_source(
        self, source: SourceRef, *, context: TrustedCaptureContext
    ) -> ChangeReceipt:
        with self._store.transaction():
            return self._statements.invalidate_source(source, context=context)

    def correct_statement(
        self,
        statement_id: str,
        replacement: StatementCandidate,
        *,
        expected_source: SourceRef,
        context: TrustedCaptureContext,
    ) -> ChangeReceipt:
        with self._store.transaction():
            return self._statements.correct_statement(
                statement_id,
                replacement,
                expected_source=expected_source,
                context=context,
            )

    def confirm_statement(
        self,
        statement_id: str,
        *,
        expected_source: SourceRef,
        evidence_ref: str,
        context: TrustedAdminContext,
    ) -> ChangeReceipt:
        with self._store.transaction():
            return self._statements.confirm_statement(
                statement_id,
                expected_source=expected_source,
                evidence_ref=evidence_ref,
                context=context,
            )

    def erase_statement(
        self, statement_id: str, *, expected_source: SourceRef, context: TrustedAdminContext
    ) -> ChangeReceipt:
        with self._store.transaction():
            return self._statements.erase_statement(
                statement_id, expected_source=expected_source, context=context
            )

    # ── jobs ─────────────────────────────────────────────────────────────────

    def enqueue_capture(
        self, sources: tuple[SourceRef, ...], *, context: TrustedCaptureContext
    ) -> CaptureJobReceipt:
        with self._store.transaction():
            return self._statements.enqueue_job(
                sources,
                context=context,
                extractor_version="pending",
                scope_key=_scope_of(sources),
            )

    def capture_job(self, job_id: str, *, context: TrustedAdminContext) -> CaptureJobReceipt:
        self._policy.require_admin(context)
        return self._statements.job_state(job_id)

    def set_capture_job_state(
        self, job_id: str, state: str, *, context: TrustedAdminContext, reason: str = ""
    ) -> CaptureJobReceipt:
        self._policy.require_admin(context)
        with self._store.transaction():
            return self._statements.set_job_state(job_id, state, reason=reason)

    def due_capture_jobs(
        self, *, now_ms: int | None = None, limit: int = 20
    ) -> tuple[CaptureJobReceipt, ...]:
        """Internal worker read: job ids only, no statement content."""
        return self._statements.due_jobs(now=now_ms, limit=limit)

    # ── administration and diagnostics ───────────────────────────────────────

    def _require_admin_context(self, context: TrustedAdminContext) -> TrustedAdminContext:
        if not isinstance(context, TrustedAdminContext):
            raise ValidationError("admin context must be a TrustedAdminContext")
        if not context.owner:
            raise KnowledgeError("unauthorized", "admin context lacks owner authority")
        self._policy.require_admin(context)
        return context

    def _require_admin_read(self, context: TrustedReadContext) -> TrustedReadContext:
        checked = self._read_context(context)
        if checked.purpose != "admin":
            raise KnowledgeError("unauthorized", "purpose must be admin for this operation")
        if not checked.owner:
            raise KnowledgeError("unauthorized", "admin inspection requires owner authority")
        self._retrieval.require_read(checked)
        return checked

    def list_statements(
        self, *, cursor: str | None, limit: int, context: TrustedAdminContext
    ) -> StatementPage:
        self._require_admin_context(context)
        if limit < 1 or limit > 100:
            raise ValidationError("limit must be within 1..100")
        return self._statements.list_statements(cursor=cursor, limit=limit)

    def inspect_statement(
        self, statement_id: str, *, context: TrustedAdminContext
    ) -> StatementSummary:
        self._require_admin_context(context)
        return self._statements.summary(statement_id)

    def update_disclosure(
        self,
        statement_id: str,
        sensitivity: str,
        mode: str,
        *,
        expected_source: SourceRef,
        context: TrustedAdminContext,
    ) -> ChangeReceipt:
        """Adjust handling inside the existing source rights - never a new grant."""
        self._require_admin_context(context)
        record = self._statements.get_statement(statement_id)
        if record is None:
            raise KnowledgeError("unresolved", f"unknown statement: {statement_id}")
        if expected_source.key not in {item[0].key for item in self._statements.sources_of(statement_id)}:
            raise KnowledgeError("invalid_input", "expected_source is not a source")
        if sensitivity not in ("normal", "sensitive", "highly_sensitive"):
            raise ValidationError("unknown sensitivity")
        if mode not in ("normal", "context_only", "never_initiate"):
            raise ValidationError("unknown disclosure mode")
        self._store.execute(
            "INSERT INTO knowledge_meta (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (f"disclosure:{statement_id}", f"{sensitivity}|{mode}"),
        )
        self._statements.audit(  # same module owns the audit trail
            statement_id,
            operation="update_disclosure",
            actor=context.actor_principal,
            reason=f"{sensitivity}|{mode}",
        )
        return ChangeReceipt(
            operation_id=self._store.new_id(),
            identity_revision=self._store.identity_revision,
            acl_epoch=self._store.bump_acl_epoch(),
            changed_ids=(statement_id,),
        )

    def reindex(self, *, context: TrustedAdminContext) -> MaintenanceReport:
        self._require_admin_context(context)
        rows = self._store.query(
            "SELECT id, content FROM memory2_nodes WHERE is_deleted = 0"
        )
        examined = len(rows)
        with self._store.transaction():
            self._store.execute("DELETE FROM memory2_nodes_fts")
            for row in rows:
                content = str(row["content"] or "")
                if content:
                    self._store.execute(
                        "INSERT INTO memory2_nodes_fts (entry_id, content) VALUES (?, ?)",
                        (str(row["id"]), content),
                    )
        return MaintenanceReport(examined=examined, changed=examined, denied=0)

    def prune(self, *, before_ms: int, context: TrustedAdminContext) -> MaintenanceReport:
        """Expire statements whose validity window passed.  Counts only, no content."""
        self._require_admin_context(context)
        examined = int(
            self._store.scalar(
                "SELECT COUNT(*) FROM knowledge_statements WHERE valid_until_ms IS NOT NULL"
                " AND valid_until_ms <= ? AND status IN ('assertion','confirmed')",
                (int(before_ms),),
            )
            or 0
        )
        with self._store.transaction():
            changed = self._statements.expire_due(now=before_ms)
        return MaintenanceReport(examined=examined, changed=changed, denied=0)

    def stats(self, *, context: TrustedAdminContext) -> KnowledgeStats:
        self._require_admin_context(context)
        raw = self._statements.stats()
        return KnowledgeStats(
            schema_version=raw["schema_version"],
            people_count=raw["people_count"],
            statement_count=raw["statement_count"],
            quarantined_count=raw["quarantined_count"],
            pending_jobs=raw["pending_jobs"],
            identity_revision=raw["identity_revision"],
            acl_epoch=raw["acl_epoch"],
            state="ready",
        )

    # ── internal helpers used by the runtime adapters ────────────────────────

    def display_name(
        self,
        person_id: str,
        *,
        context: TrustedReadContext | None = None,
        for_group: bool = False,
    ) -> str | None:
        """Eligible address for a person.  Without a context: released names only."""
        if context is None:
            return self._identity.display_name(person_id, context=None, for_group=True)
        checked = self._read_context(context)
        return self._identity.display_name(
            person_id, context=checked, for_group=for_group or not checked.is_direct
        )

    def person_for_principal(self, principal: str) -> str | None:
        """Map a security principal to a person through proven bindings only."""
        found = self._identity.person_id_for_principal(principal)
        if found is None:
            return None
        return self._identity.canonical_id(found)

    def eligible_people(
        self, *, context: TrustedReadContext
    ) -> tuple[tuple[str, str | None], ...]:
        """Proven members of the current chat with their eligible addresses."""
        checked = self._read_context(context)
        decision = self._retrieval.decide(checked)
        if not decision.allowed:
            return ()
        return self._identity.eligible_people_for_context(checked)

    def alias_names(self, person_id: str) -> tuple[str, ...]:
        """Observed aliases of a person.  Untrusted text, for display only."""
        return tuple(item.name for item in self._identity.aliases_of(person_id))

    def source_revoked(self, source: SourceRef) -> bool:
        return bool(self._authority.source_revoked(source))

    def active_statement_ids_for_source(
        self, source: SourceRef
    ) -> tuple[str, ...]:
        return self._statements.active_statement_ids_for_event(
            source.event_id, source.revision
        )
