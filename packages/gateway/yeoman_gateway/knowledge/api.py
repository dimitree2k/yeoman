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
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable

from yeoman_gateway.knowledge._conversations import ConversationEngine
from yeoman_gateway.knowledge._episodes import EpisodeConsolidator
from yeoman_gateway.knowledge._identity import IdentityEngine
from yeoman_gateway.knowledge._migration import (
    LineageImportReport,
    LineageInventory,
    import_lineage,
    inspect_lineage_sources,
)
from yeoman_gateway.knowledge._retrieval import RetrievalEngine
from yeoman_gateway.knowledge._statements import StatementEngine, token_re
from yeoman_gateway.knowledge._store import SCHEMA_VERSION, KnowledgeStore
from yeoman_gateway.knowledge.authority import (
    PolicyAuthority,
    SourceAuthority,
    wall_clock_ms,
)
from yeoman_gateway.knowledge.models import (
    CaptureJobReceipt,
    CaptureJobRecord,
    CaptureResult,
    ChangeReceipt,
    ConversationMembershipReceipt,
    ConversationMergeReceipt,
    ConversationReferenceReceipt,
    ConversationSplitReceipt,
    ConversationView,
    EndpointResolution,
    EpisodeBuildReport,
    EpisodeView,
    Identifier,
    KnowledgeContext,
    KnowledgeError,
    KnowledgeStats,
    MaintenanceReport,
    NameObservation,
    PersonLinkCandidate,
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
    normalize_alias_value,
    validate_name,
)

__all__ = [
    "KnowledgeService",
    "KnowledgeStartupError",
    "LineageImportReport",
    "LineageInventory",
    "import_lineage",
    "inspect_lineage_sources",
    "open_knowledge_store",
    "workspace_id_for",
]


class KnowledgeStartupError(KnowledgeError):
    """The knowledge store cannot be opened for the requested runtime state."""


def workspace_id_for(workspace: Path | str) -> str:
    return hashlib.sha1(str(Path(workspace).expanduser().resolve()).encode("utf-8")).hexdigest()[
        :16
    ]


@dataclass(frozen=True, slots=True)
class _SchemaProbe:
    """What an existing file says about itself, read without opening it for writing."""

    exists: bool
    has_meta: bool
    schema_version: int
    migration_complete: bool

    @property
    def is_knowledge_store(self) -> bool:
        return self.has_meta and self.schema_version > 0


def _probe_schema(db_path: Path) -> _SchemaProbe:
    """Read ``knowledge_meta`` over a read-only connection.

    The probe must never touch the file: a runtime start against a v1 store has to fail
    closed, and even opening a SQLite file read-write can create a WAL/journal and change
    its mtime.  A missing or unreadable file therefore reports "no store", and the caller
    decides separately whether that means "create a fresh one".
    """
    if not db_path.exists():
        return _SchemaProbe(exists=False, has_meta=False, schema_version=0, migration_complete=False)
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:  # pragma: no cover - defensive
        return _SchemaProbe(exists=True, has_meta=False, schema_version=0, migration_complete=False)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'knowledge_meta'"
        ).fetchone()
        if row is None:
            return _SchemaProbe(
                exists=True, has_meta=False, schema_version=0, migration_complete=False
            )
        values = {
            str(key): str(value)
            for key, value in conn.execute("SELECT key, value FROM knowledge_meta").fetchall()
        }
    except sqlite3.Error:
        return _SchemaProbe(exists=True, has_meta=False, schema_version=0, migration_complete=False)
    finally:
        conn.close()
    try:
        version = int(values.get("schema_version", "0"))
    except ValueError:
        version = -1
    return _SchemaProbe(
        exists=True,
        has_meta=True,
        schema_version=version,
        migration_complete=values.get("migration_complete", "0") == "1",
    )


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

    The order matters: the existing file is probed *read-only* before anything opens it
    for writing.  A v1 store is therefore refused byte-for-byte unchanged - no tables, no
    meta rows, no WAL side file.  Only a file that does not exist at all is created as a
    fresh v2 store; the normal start never migrates, and it never upgrades in place.
    """
    path = Path(db_path).expanduser()
    legacy = [Path(item).expanduser() for item in legacy_sources]
    probe = _probe_schema(path)
    fresh = not probe.exists
    if fresh and any(item.exists() for item in legacy):
        # The consolidated store is missing while legacy data exists.  Migrate
        # explicitly, never write an empty store next to the old data.
        raise KnowledgeStartupError(
            "migration_required",
            "legacy memory/contacts data exists but no verified knowledge store was built",
        )
    if not fresh:
        if not probe.is_knowledge_store:
            # An occupied path without a knowledge schema is not a knowledge store: for
            # example a legacy memory file configured directly as the target.
            raise KnowledgeStartupError(
                "migration_required",
                "the target file carries no knowledge schema and was not migrated",
            )
        if probe.schema_version < SCHEMA_VERSION:
            # An *older* knowledge store is not corruption: it is data that needs the
            # explicit, audited snapshot upgrade.  Anything newer or non-numeric stays
            # ``schema_incompatible``.
            raise KnowledgeStartupError(
                "migration_required",
                f"knowledge schema version {probe.schema_version} needs an explicit"
                f" snapshot upgrade to {SCHEMA_VERSION}; the normal start never migrates",
            )
        if probe.schema_version != SCHEMA_VERSION:
            raise KnowledgeStartupError(
                "schema_incompatible",
                f"knowledge schema version {probe.schema_version} is not supported"
                f" (need {SCHEMA_VERSION}); an explicit snapshot upgrade is required",
            )
        if not probe.migration_complete:
            raise KnowledgeStartupError(
                "migration_required",
                "knowledge store exists but carries no complete migration manifest;"
                " an explicit snapshot upgrade is required",
            )
    try:
        store = KnowledgeStore(path, create=create)
    except sqlite3.Error as exc:  # pragma: no cover - defensive
        raise KnowledgeStartupError("storage_unavailable", str(exc)) from exc
    if fresh and create:
        # A brand-new installation with no legacy data is complete by construction.
        store.set_meta("migration_complete", "1")
        store.set_meta("migration_id", "fresh-install")
        store.set_meta("semantic_digest", "")
        store.commit_if_idle()
    return KnowledgeService(
        store=store,
        workspace_id=workspace_id,
        source_authority=source_authority,
        policy_authority=policy_authority,
        clock=clock,
        retention_ms=retention_ms,
    )


class _RecordingSourceAuthority:
    """Wrap a read-only proof owner so an administrative source can be registered."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self._sources: dict[tuple[str, int], Any] = {}

    def register_source(self, source: SourceRef, audience: Any) -> SourceRef:
        self._sources[source.key] = source
        register = getattr(self._inner, "register_source", None)
        if register is not None:
            register(source, audience)
        return source

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def verify_source(self, source: SourceRef) -> bool:
        if source.key in self._sources:
            return self._sources[source.key] == source
        return bool(self._inner.verify_source(source))

    def verify_source_ref(self, event_id: str, revision: int) -> SourceRef | None:
        known = self._sources.get((str(event_id), int(revision)))
        if known is not None:
            return known
        return self._inner.verify_source_ref(event_id, revision)

    def source_revoked(self, source: SourceRef) -> bool:
        return bool(self._inner.source_revoked(source))

    def mark_source_revoked(self, source: SourceRef) -> None:
        self._sources.pop(source.key, None)
        marker = getattr(self._inner, "mark_source_revoked", None)
        if marker is not None:
            marker(source)

    def evidence_audience(self, source: SourceRef, *, basis: str) -> Any:
        """The audience the proof owner recorded for this revision.

        The owner's record wins whenever it has one: every registration passes its own
        audience down, so an administrative note is stored as ``author_only`` there and a
        runtime observation keeps the member list it was registered with.  Only a source
        this wrapper registered *and* the owner never recorded falls back to the narrow
        ``owner_note`` bucket.  Reading the wrapper's own map first silently turned every
        proven group audience into ``author_only``, which made the audience proof
        meaningless for promotion.
        """
        inner_audience = getattr(self._inner, "evidence_audience", None)
        if callable(inner_audience):
            found = inner_audience(source, basis=basis)
            if found is not None:
                return found
        if source.key in self._sources:
            from yeoman_gateway.knowledge.authority import EvidenceAudience

            return EvidenceAudience.author_only(snapshot_id="owner_note")
        return None


def _iso_from_ms(ms: int) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(int(ms) / 1000.0, tz=UTC).isoformat(timespec="seconds")


#: Forward cursor of statement promotion, kept in the knowledge store's meta table.
_CAPTURE_BOUNDARY_MS = "statement_capture_boundary_ms"
_CAPTURE_BOUNDARY_EVENT = "statement_capture_boundary_event_id"


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
        # One wrapper so administrative sources registered later are visible to the
        # statement engine through the very same object.
        self._authority = _RecordingSourceAuthority(source_authority)
        self._policy = policy_authority
        self.workspace_id = str(workspace_id)
        self._clock = clock
        self._identity = IdentityEngine(store, authority=self._authority, policy=policy_authority)
        self._statements = StatementEngine(
            store,
            identity=self._identity,
            authority=self._authority,
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
        self._conversations = ConversationEngine(
            store,
            authority=self._authority,
            retrieval=self._retrieval,
            workspace_id=self.workspace_id,
        )
        self._episodes = EpisodeConsolidator(
            store,
            authority=self._authority,
            retrieval=self._retrieval,
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

    @property
    def policy_revision(self) -> int:
        """The Policy revision this service validates its trusted contexts against."""
        return int(self._policy.current_policy_revision())

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

    def recall(
        self, query: RecallQuery, *, context: TrustedReadContext, view: str = "current"
    ) -> KnowledgeContext:
        """Recall under the one shared read contract.

        ``view`` selects the contract row of §7.5: ``current`` (the default), ``historic``
        (a proven earlier period of a ``state_change``), ``correction_audit`` (retracted
        claims, for an authorized correction history) or ``diagnosis`` (authorized
        inspection, still never ``revoked`` content).
        """
        checked = self._read_context(context)
        return self._retrieval.recall(query, context=checked, view=view)

    def recall_hybrid(
        self,
        query: RecallQuery,
        *,
        context: TrustedReadContext,
        embedder: Any | None = None,
        preprocessing_version: str | None = None,
    ) -> KnowledgeContext:
        """Automatic context and recall: exact/FTS first, vectors only as an addition.

        Uses the same read gate as :meth:`recall`; a missing or failing provider can only
        reduce recall, never hide a lexically findable statement.  The preprocessing
        version is bound so a row produced by another preprocessing is never merged.
        """
        checked = self._read_context(context)
        return self._retrieval.recall_hybrid(
            query,
            context=checked,
            embedder=embedder,
            preprocessing_version=preprocessing_version,
        )

    def profile(
        self, person_id: str, *, context: TrustedReadContext, view: str = "current"
    ) -> PersonProfile:
        checked = self._read_context(context)
        return self._retrieval.profile(person_id, context=checked, view=view)

    def person_profile(
        self,
        person_id: str,
        *,
        context: TrustedReadContext | None,
        view: str = "current",
        history_at_ms: int | None = None,
    ) -> Any:
        """The deterministic, bounded person card for one authorized reader.

        Read-only by construction: no backfill, no merge, no model call, nothing stored.
        ``history_at_ms`` renders the card as of a proven past instant, which only ever
        shows a ``state_change`` inside its own proven period.
        """
        checked = None if context is None else self._read_context(context)
        return self._retrieval.person_profile(
            person_id, context=checked, view=view, history_at_ms=history_at_ms
        )

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

    # ── temporal bindings, aliases and platform observations ────────────────

    def resolve_observation(
        self,
        observation: TrustedIdentityObservation,
        *,
        context: TrustedReadContext | None = None,
        create_stub: bool = True,
    ) -> PersonResolution:
        """Resolve a verified platform observation to exactly one person.

        The public spelling of the identity path.  Ambiguity, an unproven multi-identifier
        mapping and two people claiming one identifier are results, never a first-match
        merge.  A verified unknown platform identity may create a stub; model text never
        reaches this method.
        """
        with self._store.transaction():
            return self._identity.resolve_observation(
                observation, context=context, create_stub=create_stub
            )

    def resolve_identifier(
        self, identifier: Identifier, *, at_ms: int | None = None
    ) -> EndpointResolution:
        """Resolve one fully typed identifier, optionally at a proven past instant."""
        return self._identity.resolve_identifier(identifier, at_ms=at_ms)

    def add_or_end_binding(
        self,
        *,
        person_id: str,
        identifier: Identifier,
        evidence_ref: str,
        context: TrustedAdminContext,
        expected_revision: int | None = None,
        mapping_verified: bool = True,
        valid_from_ms: int | None = None,
        end_binding_id: str | None = None,
        end_at_ms: int | None = None,
    ) -> ChangeReceipt:
        """Claim, extend or hand over one temporal identifier binding."""
        with self._store.transaction():
            return self._identity.add_or_end_binding(
                person_id=person_id,
                identifier=identifier,
                evidence_ref=evidence_ref,
                mapping_verified=mapping_verified,
                context=context,
                expected_revision=(
                    self._store.identity_revision
                    if expected_revision is None
                    else int(expected_revision)
                ),
                valid_from_ms=valid_from_ms,
                end_binding_id=end_binding_id,
                end_at_ms=end_at_ms,
            )

    def end_binding(
        self,
        *,
        binding_id: str,
        context: TrustedAdminContext,
        expected_revision: int | None = None,
        end_at_ms: int | None = None,
    ) -> ChangeReceipt:
        """End exactly one binding; its proven period stays as history."""
        with self._store.transaction():
            return self._identity.end_binding(
                binding_id=binding_id,
                expected_revision=(
                    self._store.identity_revision
                    if expected_revision is None
                    else int(expected_revision)
                ),
                context=context,
                end_at_ms=end_at_ms,
            )

    def observe_alias(
        self,
        *,
        person_id: str,
        name: str,
        alias_kind: str = "other_name",
        scope_key: str = "",
        evidence_ref: str = "",
        status: str = "observed",
        address_allowed: bool = False,
        supporting_statement_id: str | None = None,
        visibility: str = "public",
        valid_until_ms: int | None = None,
        source: str = "observed",
    ) -> NameObservation:
        """Record one alias with an explicit kind, context and evidence."""
        with self._store.transaction():
            return self._identity.observe_alias(
                person_id=person_id,
                name=name,
                alias_kind=alias_kind,
                scope_key=scope_key,
                evidence_ref=evidence_ref,
                status=status,
                address_allowed=address_allowed,
                supporting_statement_id=supporting_statement_id,
                visibility=visibility,
                valid_until_ms=valid_until_ms,
                source=source,
            )

    def set_alias_preference(
        self,
        *,
        alias_id: int,
        context: TrustedAdminContext,
        expected_revision: int | None = None,
        address_allowed: bool = True,
    ) -> ChangeReceipt:
        """Make one alias the preferred address in its own context."""
        with self._store.transaction():
            return self._identity.set_alias_preference(
                alias_id=int(alias_id),
                expected_revision=(
                    self._store.identity_revision
                    if expected_revision is None
                    else int(expected_revision)
                ),
                context=context,
                address_allowed=address_allowed,
            )

    def retire_alias(
        self,
        *,
        alias_id: int,
        context: TrustedAdminContext,
        expected_revision: int | None = None,
        reason: str = "not_wanted",
        correct_mapping: bool = False,
    ) -> ChangeReceipt:
        """Withdraw the addressing permission, or retract the mapping itself."""
        with self._store.transaction():
            return self._identity.retire_alias(
                alias_id=int(alias_id),
                expected_revision=(
                    self._store.identity_revision
                    if expected_revision is None
                    else int(expected_revision)
                ),
                context=context,
                reason=reason,
                correct_mapping=correct_mapping,
            )

    def aliases_of(self, person_id: str) -> tuple[NameObservation, ...]:
        """Every stored alias of a person, including retired ones (for inspection)."""
        return self._identity.aliases_of(person_id)

    def address_aliases_of(
        self, person_id: str, *, scope_key: str | None = None
    ) -> tuple[NameObservation, ...]:
        """Aliases that may be used to address a person, preferred first."""
        return self._identity.address_aliases_of(person_id, scope_key=scope_key)

    # ── statement lifecycle ──────────────────────────────────────────────────

    def invalidate_source(
        self, source: SourceRef, *, context: TrustedCaptureContext
    ) -> ChangeReceipt:
        with self._store.transaction():
            return self._statements.invalidate_source(source, context=context)

    def invalidate_event_sources(
        self, event_ids: Iterable[str], *, reason: str = "source_revoked"
    ) -> tuple[str, ...]:
        """Apply an already-projected provider revocation to derived statements.

        This is the internal revocation projection, called by the journal owner *after*
        it authorized and persisted the revocation.  It grants nothing: it only makes the
        statements that rested on the revoked revision stop being readable.
        """
        with self._store.transaction():
            return self._statements.invalidate_event_ids(event_ids, reason=str(reason))

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

    def end_attribute(
        self,
        *,
        statement_id: str,
        person_id: str,
        attribute_key: str,
        context: TrustedAdminContext,
        expected_revision: int | None = None,
        reason: str = "ended_by_owner",
    ) -> ChangeReceipt:
        """End one facet by superseding the statement that carries it."""
        with self._store.transaction():
            return self._statements.end_attribute(
                statement_id=statement_id,
                person_id=person_id,
                attribute_key=attribute_key,
                expected_revision=(
                    self._store.identity_revision
                    if expected_revision is None
                    else int(expected_revision)
                ),
                context=context,
                reason=reason,
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
        self,
        sources: tuple[SourceRef, ...],
        *,
        context: TrustedCaptureContext,
        scope_key: str | None = None,
        extractor_version: str = "pending",
        max_waiting: int | None = None,
        due_ms: int | None = None,
        ts_ms: int | None = None,
    ) -> CaptureJobReceipt:
        with self._store.transaction():
            return self._statements.enqueue_job(
                sources,
                context=context,
                extractor_version=str(extractor_version),
                scope_key=str(scope_key or _scope_of(sources)),
                max_waiting=max_waiting,
                due_ms=due_ms,
                ts_ms=ts_ms,
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

    # The remaining job calls are internal worker reads and writes.  They deliberately
    # skip the admin context: the worker is not an administrative actor, and none of them
    # returns statement content.  ``set_capture_job_state`` above stays the audited admin
    # path for manual intervention.

    def capture_job_record(self, job_id: str) -> CaptureJobRecord:
        """Internal worker read: one job with its resolvable sources."""
        return self._statements.job_record(job_id)

    def stale_capture_jobs(
        self, *, updated_before_ms: int, limit: int = 50
    ) -> tuple[CaptureJobReceipt, ...]:
        """Internal worker read: jobs a crash left ``running``."""
        return self._statements.stale_jobs(updated_before_ms=updated_before_ms, limit=limit)

    def mark_capture_job(
        self, job_id: str, state: str, *, reason: str = "", ts_ms: int | None = None
    ) -> CaptureJobReceipt:
        """Internal worker write: record the terminal state of one job."""
        with self._store.transaction():
            return self._statements.set_job_state(job_id, state, reason=reason, ts_ms=ts_ms)

    def requeue_capture_job(
        self, job_id: str, *, due_ms: int, reason: str = "", ts_ms: int | None = None
    ) -> CaptureJobReceipt:
        """Internal worker write: bounded retry of a failed job."""
        with self._store.transaction():
            return self._statements.requeue_job(
                job_id, due_ms=due_ms, reason=reason, ts_ms=ts_ms
            )

    def capture_status(self, *, now_ms: int | None = None) -> dict[str, Any]:
        """Read-only capture counters; never contains statement content."""
        moment = int(now_ms if now_ms is not None else self._store.now_ms())
        return self._statements.status(now_ms=moment)

    def capture_boundary(self) -> tuple[int, str] | None:
        """The durable forward cursor of statement capture, if one was set."""
        raw_ms = self._store.get_meta(_CAPTURE_BOUNDARY_MS)
        if raw_ms is None:
            return None
        try:
            moment = int(raw_ms)
        except (TypeError, ValueError):
            return None
        return (moment, str(self._store.get_meta(_CAPTURE_BOUNDARY_EVENT, "") or ""))

    def set_capture_boundary(self, after_ms: int, after_event_id: str = "") -> tuple[int, str]:
        """Persist the forward cursor.  Only the promotion worker writes it."""
        moment = max(0, int(after_ms))
        self._store.set_meta(_CAPTURE_BOUNDARY_MS, str(moment))
        self._store.set_meta(_CAPTURE_BOUNDARY_EVENT, str(after_event_id or ""))
        return (moment, str(after_event_id or ""))

    # ── conversation threads ─────────────────────────────────────────────────
    #
    # A thread is a subject-matter statement, not a runtime route.  Membership is keyed
    # by ``(conversation_id, source_event_id, source_revision)`` and stores references,
    # never text, so one source revision may join several threads.  Every read runs
    # through the same gate as statements; a thread never widens chat access.

    def attach_conversation_membership(
        self,
        source: SourceRef,
        *,
        context: TrustedCaptureContext,
        conversation_id: str | None = None,
        origin: str = "manual",
        confidence: float = 1.0,
        classifier_version: str = "",
        text_offsets: tuple[int, int] | None = None,
    ) -> ConversationMembershipReceipt:
        """Attach one proven source revision to a thread, creating the thread if needed."""
        return self._conversations.attach_membership(
            source,
            context=context,
            conversation_id=conversation_id,
            origin=origin,
            confidence=confidence,
            classifier_version=classifier_version,
            text_offsets=text_offsets,
            now_ms=self._now(),
        )

    def record_conversation_reference(
        self,
        source: SourceRef,
        *,
        refers_to: SourceRef,
        context: TrustedCaptureContext,
        kind: str = "reply",
        confidence: float = 1.0,
        classifier_version: str = "explicit-reference-v1",
    ) -> ConversationReferenceReceipt:
        """Record an explicit reply/quote as a relation candidate between two threads."""
        return self._conversations.record_reference(
            source,
            refers_to=refers_to,
            context=context,
            kind=kind,
            confidence=confidence,
            classifier_version=classifier_version,
            now_ms=self._now(),
        )

    def split_conversation(
        self,
        conversation_id: str,
        *,
        sources: tuple[SourceRef, ...],
        context: TrustedCaptureContext,
        origin: str = "split",
        confidence: float = 1.0,
        classifier_version: str = "",
    ) -> ConversationSplitReceipt:
        """Branch a new thread off an existing one; the prior ids and rows survive."""
        return self._conversations.split_conversation(
            conversation_id,
            sources=sources,
            context=context,
            origin=origin,
            confidence=confidence,
            classifier_version=classifier_version,
            now_ms=self._now(),
        )

    def merge_conversations(
        self,
        source_ids: tuple[str, ...],
        *,
        target_id: str,
        context: TrustedCaptureContext,
    ) -> ConversationMergeReceipt:
        """Merge threads by redirect: retired ids stay resolvable and auditable."""
        return self._conversations.merge_conversations(
            source_ids, target_id=target_id, context=context, now_ms=self._now()
        )

    def conversation(
        self, conversation_id: str, *, context: TrustedReadContext
    ) -> ConversationView:
        """The gated read projection of one thread, derived from relational rows."""
        return self._conversations.view(conversation_id, context=self._read_context(context))

    def conversations_for_source(
        self, source: SourceRef, *, context: TrustedReadContext
    ) -> tuple[ConversationView, ...]:
        """Every thread one source revision belongs to, gated per thread."""
        return self._conversations.conversations_for_source(
            source, context=self._read_context(context)
        )

    # ── episodes ─────────────────────────────────────────────────────────────
    #
    # An episode is a derived statement about closed context, with complete source and
    # statement provenance.  It is never independent human evidence, it is never disclosed
    # more broadly than every one of its sources permits, and rebuilding keeps the prior
    # version for audit.

    def consolidate_episodes(
        self,
        *,
        scope_key: str,
        context: TrustedAdminContext,
        summarizer: Any | None = None,
        now_ms: int | None = None,
        model_version: str | None = None,
        prompt_version: str | None = None,
    ) -> EpisodeBuildReport:
        """Build or reuse the active episode of one chat scope.  Administrative."""
        self._require_admin_context(context)
        from yeoman_gateway.knowledge._episodes import (
            DEFAULT_MODEL_VERSION,
            EPISODE_PROMPT_VERSION,
        )

        return self._episodes.consolidate(
            scope_key=str(scope_key),
            summarizer=summarizer,
            now_ms=self._now() if now_ms is None else int(now_ms),
            model_version=model_version or DEFAULT_MODEL_VERSION,
            prompt_version=prompt_version or EPISODE_PROMPT_VERSION,
        )

    def episodes(
        self, *, scope_key: str, context: TrustedReadContext
    ) -> tuple[EpisodeView, ...]:
        """Every episode of the reader's own scope, gated and staleness-checked."""
        return self._episodes.list_episodes(
            scope_key=str(scope_key), context=self._read_context(context)
        )

    def episode(self, episode_id: str, *, context: TrustedReadContext) -> EpisodeView:
        """One episode, including a superseded version kept for audit."""
        return self._episodes.get(episode_id, context=self._read_context(context))

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

    # ── private storage bindings for the internal runtime adapters ───────────
    #
    # These return sub-stores that *join* this service's connection.  They exist for the
    # two internal adapters (session notes/backlog and the contacts cache) and for the
    # composition root.  They are not part of the consumer contract: external callers
    # use the typed methods above and never receive a store handle.

    def memory_store(self) -> Any:
        """The session/notes adapter's view of the shared store.  Internal use only."""
        from yeoman_gateway.knowledge._memory.store import MemoryStore

        return MemoryStore(owner=self._store)

    def contacts_store(self) -> Any:
        """The contacts cache adapter's view of the shared store.  Internal use only."""
        from yeoman_gateway.knowledge._contacts.store import ContactsStore

        return ContactsStore(owner=self._store)

    # ── internal helpers used by the runtime adapters ────────────────────────

    def register_turn_source(
        self,
        *,
        source: SourceRef,
        verified_members: frozenset[str],
        snapshot_id: str,
        author_only: bool = False,
    ) -> bool:
        """Register one archived turn revision as proven evidence.

        The caller proves *who was in the chat* (its own registry decision); the module
        turns that into the audience record.  Registering evidence is not a capture: no
        statement is published and no read right is granted.
        """
        from yeoman_gateway.knowledge.authority import EvidenceAudience

        register = getattr(self._authority, "register_source", None)
        if register is None or not source.event_id:
            return False
        if author_only:
            register(source, EvidenceAudience.author_only(snapshot_id=snapshot_id))
            return True
        if not verified_members:
            return False
        register(
            source,
            EvidenceAudience.known(frozenset(verified_members), snapshot_id=snapshot_id),
        )
        return True

    @property
    def knowledge_sources(self) -> Any:
        """The proof owner for archived sources (the registered source authority)."""
        return self._authority

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

    def recall_context(
        self,
        text: str,
        *,
        context: TrustedReadContext,
        reply_to_text: str | None = None,
        reply_to_person: str | None = None,
        limit: int = 12,
    ) -> KnowledgeContext:
        """Scoped recall of ordinary, non-personal memories for one verified reader.

        This is the only read path for memories that carry no statement shell.  It is
        *not* a fallback around the statement gate: the caller has already been checked
        against current membership and audience, and the returned rows are restricted to
        scopes derived server-side from that same verified context.
        """
        checked = self._read_context(context)
        decision = self._retrieval.decide(checked)
        if not decision.allowed:
            return KnowledgeContext(
                reason=decision.reason,
                identity_revision=self._store.identity_revision,
                acl_epoch=self._store.acl_epoch,
            )
        query = " ".join(str(text or "").split()) or " ".join(str(reply_to_text or "").split())
        if not query:
            return KnowledgeContext(
                reason="empty",
                identity_revision=self._store.identity_revision,
                acl_epoch=self._store.acl_epoch,
            )
        scope_keys = self._allowed_scope_keys(checked, decision, reply_to_person)
        if not scope_keys:
            return KnowledgeContext(
                reason="empty",
                identity_revision=self._store.identity_revision,
                acl_epoch=self._store.acl_epoch,
            )
        placeholders = ",".join("?" for _ in scope_keys)
        tokens = [token for token in token_re().findall(query.lower()) if len(token) > 1][:16]
        if not tokens:
            return KnowledgeContext(
                reason="empty",
                identity_revision=self._store.identity_revision,
                acl_epoch=self._store.acl_epoch,
            )
        match = " OR ".join(tokens)
        rows = self._store.query(
            "SELECT n.id AS id, n.content AS content FROM memory2_nodes_fts"
            " JOIN memory2_nodes n ON n.id = memory2_nodes_fts.entry_id"
            f" WHERE n.workspace_id = ? AND n.is_deleted = 0 AND n.scope_key IN ({placeholders})"
            " AND memory2_nodes_fts MATCH ?"
            " AND n.id NOT IN (SELECT statement_id FROM knowledge_statements WHERE status <> 'revoked')"
            " ORDER BY bm25(memory2_nodes_fts) ASC, n.updated_at DESC LIMIT ?",
            (self.workspace_id, *scope_keys, match, int(max(1, min(limit, 50)))),
        )
        lines: list[str] = []
        ids: list[str] = []
        for row in rows:
            content = str(row["content"] or "").strip()
            if not content:
                continue
            lines.append(content)
            ids.append(str(row["id"]))
        return KnowledgeContext(
            text="\n".join(lines),
            statement_ids=tuple(ids),
            identity_revision=self._store.identity_revision,
            acl_epoch=self._store.acl_epoch,
            context_revision=f"legacy:{self._store.acl_epoch}:{len(ids)}",
            reason="ok" if ids else "empty",
        )

    def _allowed_scope_keys(
        self,
        context: TrustedReadContext,
        decision: Any,
        reply_to_person: str | None,
    ) -> tuple[str, ...]:
        """Scope keys this exact reader may search, derived from the verified context."""
        keys = [context.scope_key()]
        keys.append(f"channel:{context.channel}:user:{context.principal_id.split(':')[-1]}")
        keys.append(f"workspace:{self.workspace_id}:global")
        for candidate in (reply_to_person,):
            if not candidate:
                continue
            person_id = self.person_for_principal(str(candidate)) or self._identity.canonical_id(
                str(candidate)
            )
            if person_id and self._identity.get_person(person_id) is not None:
                keys.append(f"contact:{person_id}")
        principal_person = self.person_for_principal(context.principal_id)
        if principal_person:
            keys.append(f"contact:{principal_person}")
        return tuple(dict.fromkeys(keys))

    def admin_context_for(self, *, reason: str) -> TrustedAdminContext:
        """Issue an admin context from Policy's own owner decision.

        Runtime surfaces (tools, CLI) never build admin authority from their arguments;
        they ask here and Policy decides.
        """
        actor = self._policy.admin_actor() if hasattr(self._policy, "admin_actor") else ""
        if not actor:
            raise KnowledgeError("unauthorized", "no owner actor is available from policy")
        return TrustedAdminContext(
            actor_principal=str(actor),
            policy_revision=self.policy_revision,
            authorization_ref=f"policy:{reason}",
            owner=True,
        )

    def maintenance_scope_keys(
        self,
        *,
        scope: str,
        channel: str | None = None,
        chat_id: str | None = None,
        sender_id: str | None = None,
    ) -> tuple[str, ...]:
        """Scope keys for an authorized maintenance query.

        Administrative diagnostics may filter by scope; the key layout stays inside the
        module so no consumer builds these strings itself.
        """
        keys: list[str] = []
        if scope in {"chat", "all"} and channel and chat_id:
            keys.append(f"channel:{channel}:chat:{chat_id}")
        if scope in {"user", "all"} and channel and (sender_id or chat_id):
            keys.append(f"channel:{channel}:user:{(sender_id or chat_id or '').strip()}")
        if scope in {"global", "all"}:
            keys.append(f"workspace:{self.workspace_id}:global")
        return tuple(keys)

    def bind_identifier_for_migration(
        self,
        *,
        person_id: str,
        channel: str,
        kind: str,
        value: str,
        namespace: str | None = None,
    ) -> None:
        """Record one identifier during the transitional legacy co-existence.

        Only the composition/transitional path uses this.  It writes the compatibility
        projection and an *unproven candidate* binding with ``legacy-import`` evidence; it
        never mints an active authority, because no channel adapter proved the mapping.
        A later audited admin operation or a real platform observation promotes it.
        """
        identifier = Identifier(
            channel=str(channel), kind=str(kind), value=str(value), namespace=namespace
        )
        self._identity.record_unproven_identifier(
            person_id=str(person_id), identifier=identifier, evidence_ref="legacy-import"
        )
        self._store.commit_if_idle()

    def promote_legacy_person(
        self,
        *,
        person_id: str,
        display_name: str,
        identifiers: tuple[Any, ...] = (),
        aliases: tuple[Any, ...] = (),
        fields: tuple[Any, ...] = (),
    ) -> None:
        """Register a person that already exists in the legacy contacts layout.

        Used by the transitional runtime while the two layouts coexist: the ids are
        preserved, names become observed aliases and profile text stays quarantined
        instead of being promoted to a readable statement.
        """
        from yeoman_gateway.knowledge._store import QUARANTINE_REASONS

        ts = self._now()
        self._store.execute(
            "INSERT INTO contacts (id, display_name, phone_number, is_owner, created_at,"
            " updated_at, revision, status, preferred_name_visibility)"
            " VALUES (?, ?, NULL, 0, ?, ?, 1, 'active', 'public')"
            " ON CONFLICT(id) DO UPDATE SET display_name = excluded.display_name,"
            " updated_at = excluded.updated_at",
            (str(person_id), str(display_name), _iso_from_ms(ts), _iso_from_ms(ts)),
        )
        for item in identifiers:
            channel = str(getattr(item, "channel", "") or "")
            value = str(getattr(item, "identifier", "") or "")
            kind = str(getattr(item, "kind", "") or "handle")
            namespace = str(getattr(item, "namespace", "") or "") or None
            if not channel or not value:
                continue
            try:
                identifier = Identifier(
                    channel=channel, kind=kind, value=value, namespace=namespace
                )
            except KnowledgeError:
                # An unparseable legacy identifier is not silently reinterpreted.
                continue
            self._identity.record_unproven_identifier(
                person_id=str(person_id),
                identifier=identifier,
                evidence_ref="legacy-import",
            )
        for item in aliases:
            alias = str(getattr(item, "alias", "") or "")
            source = str(getattr(item, "source", "") or "observed")
            if not alias:
                continue
            try:
                clean = validate_name(alias)
            except KnowledgeError:
                continue
            self._store.execute(
                "INSERT INTO contact_aliases (contact_id, alias, source, first_seen,"
                " last_seen, normalized_alias, scope_key, status, address_allowed,"
                " is_preferred, revision)"
                " VALUES (?, ?, ?, ?, ?, ?, 'global', 'observed', 0, 0, 1)"
                " ON CONFLICT(contact_id, alias, source) DO NOTHING",
                (
                    str(person_id),
                    clean,
                    source,
                    _iso_from_ms(ts),
                    _iso_from_ms(ts),
                    normalize_alias_value(clean),
                ),
            )
        for item in fields:
            value = str(getattr(item, "value", "") or "")
            if not value:
                continue
            # Legacy profile text has no proven source or ACL: keep it quarantined.
            self._store.execute(
                "INSERT INTO knowledge_quarantine (quarantine_id, source_table, source_pk,"
                " reason, detail_json, created_ms) VALUES (?, 'contact_fields', ?, ?, '{}', ?)"
                " ON CONFLICT(source_table, source_pk, reason) DO NOTHING",
                (
                    self._store.new_id(),
                    f"{person_id}:{getattr(item, 'kind', '')}",
                    QUARANTINE_REASONS[0],
                    ts,
                ),
            )
        self._store.commit_if_idle()

    def person_id_for_value(self, value: str) -> str | None:
        """Person id for a proven *active* identifier value, searched across channels.

        The compatibility projection ``contact_identifiers`` is deliberately not a
        fallback here: a legacy row without a proven mapping must not resolve a person.
        """
        token = str(value or "").strip()
        if not token:
            return None
        candidates: list[str] = [token]
        local = token.split("@", 1)[0]
        if local and local != token:
            candidates.append(local)
        for candidate in dict.fromkeys(candidates):
            row = self._store.query_one(
                "SELECT person_id FROM knowledge_identifier_bindings"
                " WHERE value = ? AND status = 'active' LIMIT 1",
                (candidate,),
            )
            if row is not None:
                return self.canonical_id(str(row["person_id"]))
        return None

    def canonical_id(self, person_id: str) -> str:
        """Current canonical person for an original (possibly merged) person id."""
        return self._identity.canonical_id(person_id)

    def name_for_identifier(self, value: str, *, for_group: bool = True) -> str | None:
        """Display name of the person a proven identifier belongs to.

        Rendering helper for history and reply context.  ``for_group`` keeps names that
        were only released for a direct conversation out of group output.
        """
        person_id = self.person_id_for_value(value)
        if person_id is None:
            return None
        return self.display_name(person_id, for_group=for_group)

    def identifier_for_name(
        self,
        name: str,
        *,
        channel: str,
        prefer: tuple[str, ...] = (),
    ) -> Identifier | None:
        """One delivery identifier for an exact name or alias.

        Refuses ambiguity: two people with the same name yield ``None`` instead of a
        first match, and an optional ``prefer`` list narrows to identifiers already
        present in the current conversation.
        """
        query = str(name or "").strip()
        if not query:
            return None
        rows = self._store.query(
            """
            SELECT DISTINCT c.id AS id FROM contacts c
             LEFT JOIN contact_aliases a ON a.contact_id = c.id
             WHERE c.status = 'active'
               AND (c.display_name = ? COLLATE NOCASE
                    OR c.preferred_name = ? COLLATE NOCASE
                    OR a.alias = ? COLLATE NOCASE)
             ORDER BY c.id
            """,
            (query, query, query),
        )
        people = [self.canonical_id(str(row["id"])) for row in rows]
        people = list(dict.fromkeys(people))
        if len(people) != 1:
            return None
        resolution = self._identity.resolve_endpoint(people[0], str(channel))
        candidates = [resolution.identifier] if resolution.identifier else []
        if not candidates:
            bindings = self._identity.active_bindings_of(people[0])
            candidates = [
                item.identifier for item in bindings if item.identifier.channel == str(channel)
            ]
        if not candidates:
            return None
        if prefer:
            preferred = [item for item in candidates if item.value in prefer]
            if len(preferred) == 1:
                return preferred[0]
        if len(candidates) == 1:
            return candidates[0]
        return None

    def person_identifiers(self, person_id: str) -> tuple[Identifier, ...]:
        """Proven identifier bindings of a person, for diagnostics and tools."""
        return tuple(item.identifier for item in self._identity.active_bindings_of(person_id))

    def record_note(
        self,
        person_id: str,
        *,
        content: str,
        label: str | None = None,
        channel: str = "cli",
        chat_id: str = "cli",
    ) -> ChangeReceipt:
        """Record an administrator's hand-written note as a real statement.

        The note receives a persistent administrative source receipt and its audience is
        the owner only - never derived from free text and never widened by a person link.
        """
        context = self.admin_context_for(reason=f"note:{label or 'manual'}")
        person = self._identity.canonical_id(person_id)
        if self._identity.get_person(person) is None:
            raise KnowledgeError("unresolved", f"unknown person: {person_id}")
        source = SourceRef(
            event_id=f"admin-note:{context.authorization_ref}:{self._store.new_id()}",
            revision=1,
            channel=str(channel),
            chat_id=str(chat_id),
            author_principal=context.actor_principal,
            occurred_at_ms=self._now(),
        )
        from yeoman_gateway.knowledge.authority import EvidenceAudience

        self._authority.register_source(
            source, EvidenceAudience.author_only(snapshot_id="owner_note")
        )
        candidate = StatementCandidate(
            content=str(content),
            sources=(source,),
            people=(
                PersonLinkCandidate(
                    person_id=person,
                    role="subject",
                    source=source,
                    attribution="confirmed",
                ),
            ),
            extractor_version="admin-note-v1",
            confidence=1.0,
            kind="note",
        )
        capture_context = TrustedCaptureContext(
            request_id=f"admin-note:{source.event_id}",
            policy_revision=self.policy_revision,
            capture_basis="owner_private_note",
            authorized_sources=(source,),
            actor_principal=context.actor_principal,
            authorized=True,
            admin_initiated=True,
        )
        with self._store.transaction():
            result = self._statements.capture(candidate, context=capture_context)
        return ChangeReceipt(
            operation_id=source.event_id,
            identity_revision=self._store.identity_revision,
            acl_epoch=self._store.acl_epoch,
            changed_ids=result.statement_ids,
        )

    def erase_matching_statements(
        self, person_id: str, *, contains: str, reason: str = "admin"
    ) -> int:
        """Erase the statements about a person whose text contains a token.

        Used by the administrative remove-field action: the caller names the content,
        the service decides which authorized statements actually match.
        """
        needle = str(contains or "").strip()
        if not needle:
            return 0
        rows = self._store.query(
            "SELECT s.statement_id, n.content, src.event_id, src.revision, src.channel,"
            " src.chat_id, src.author_principal, src.occurred_at_ms"
            " FROM knowledge_statements s"
            " JOIN memory2_nodes n ON n.id = s.statement_id"
            " JOIN knowledge_statement_sources src ON src.statement_id = s.statement_id"
            " WHERE s.status <> 'revoked' AND n.content LIKE ?"
            " AND s.statement_id IN (SELECT statement_id FROM knowledge_statement_people"
            "                        WHERE person_id = ?)",
            (f"%{needle}%", str(person_id)),
        )
        context = self.admin_context_for(reason=reason)
        erased = 0
        for row in rows:
            expected = SourceRef(
                event_id=str(row["event_id"]),
                revision=int(row["revision"]),
                channel=str(row["channel"]),
                chat_id=str(row["chat_id"]),
                author_principal=str(row["author_principal"]),
                occurred_at_ms=int(row["occurred_at_ms"]),
            )
            with self._store.transaction():
                self._statements.erase_statement(
                    str(row["statement_id"]), expected_source=expected, context=context
                )
            erased += 1
        return erased

    def merge_people_with_policy(
        self, target_id: str, source_id: str, *, reason: str = "admin"
    ) -> ChangeReceipt:
        """Reversible merge with a Policy-issued admin context."""
        context = self.admin_context_for(reason=reason)
        with self._store.transaction():
            return self._identity.merge_people(
                target_id,
                source_id,
                expected_revision=self._store.identity_revision,
                context=context,
            )

    def search_people_with_policy(
        self,
        name: str,
        *,
        channel: str = "whatsapp",
        chat_id: str = "cli",
        purpose: str = "admin",
    ) -> tuple[PersonResolution, ...]:
        """Name lookup for authorized surfaces; several people may share a name."""
        context = TrustedReadContext(
            principal_id=self._policy.admin_actor()
            if hasattr(self._policy, "admin_actor")
            else "owner",
            channel=str(channel),
            chat_id=str(chat_id),
            recipient_principals=frozenset(
                {
                    self._policy.admin_actor()
                    if hasattr(self._policy, "admin_actor")
                    else "owner"
                }
            ),
            membership_revision="admin",
            policy_revision=self.policy_revision,
            purpose=purpose if purpose in ("reply", "proactive", "profile", "admin") else "admin",
            now_ms=self._now(),
            is_direct=True,
            owner=True,
        )
        return self._identity.search_by_name(name, context=context)

    def person_facts(
        self,
        person_id: str,
        *,
        context: TrustedReadContext | None = None,
        limit: int = 50,
    ) -> tuple[tuple[str, str, str | None], ...]:
        """Released profile facts of a person: (kind, value, label).

        A projection over permitted statements, never a copy stored in a contact field -
        and never a bypass around the read gate.  Without a trusted read context there is
        no answer at all: the previous version happily returned raw rows for any caller,
        which is exactly the "some recall exists somewhere" hole this contract closes.
        """
        if context is None:
            return ()
        checked = self._read_context(context)
        bounded = max(1, min(int(limit), 50))
        result = self.recall(
            RecallQuery(person_ids=(person_id,), limit=bounded), context=checked
        )
        if not result.statement_ids:
            return ()
        facts: list[tuple[str, str, str | None]] = []
        for statement_id in result.statement_ids:
            summary = self._statements.get_statement(statement_id)
            if summary is None or not summary.content:
                continue
            facts.append(("statement", summary.content, summary.status))
        return tuple(facts)

    def set_preferred_name_with_policy(
        self,
        person_id: str,
        name: str,
        *,
        reason: str = "tool",
        visibility: str = "public",
    ) -> ChangeReceipt:
        """Owner path for name changes requested from an authorized runtime surface.

        The admin context is issued here from the Policy authority's own owner decision -
        never from tool arguments - and the caller only supplies the person and the name.
        """
        actor = self._policy.admin_actor() if hasattr(self._policy, "admin_actor") else ""
        if not actor:
            raise KnowledgeError("unauthorized", "no owner actor is available from policy")
        context = TrustedAdminContext(
            actor_principal=str(actor),
            policy_revision=self.policy_revision,
            authorization_ref=f"policy:{reason}",
            owner=True,
        )
        return self.set_preferred_name(person_id, name, context=context, visibility=visibility)

    def roster_for_chat(
        self,
        *,
        context: TrustedReadContext,
        participant_ids: tuple[str, ...] = (),
    ) -> tuple[tuple[str, tuple[str, ...]], ...]:
        """Backwards-compatible alias of :meth:`roster` with an explicit tuple."""
        return self.roster(context=context, participant_ids=participant_ids)

    def identifier_for_principal(
        self, principal: str, *, prefer_kind: str | None = None
    ) -> Identifier | None:
        """The proven identifier of one principal, or ``None``.

        Replaces direct lookups in the old in-memory identifier cache; the answer comes
        from stored bindings, not from a name.
        """
        person_id = self.person_for_principal(principal)
        if person_id is None:
            return None
        channel = str(principal).partition(":")[0] or "whatsapp"
        resolution = self._identity.resolve_endpoint(person_id, channel, prefer_kind=prefer_kind)
        return resolution.identifier

    def person_display_name(
        self, person_id: str, *, context: TrustedReadContext | None = None
    ) -> str | None:
        """Eligible address of a person.  Without a context: released names only."""
        return self.display_name(person_id, context=context)

    def known_identifier_values(self) -> tuple[str, ...]:
        """Every identifier value the runtime knows, without exposing owner mapping.

        Used for alias matching in delivery decisions; it deliberately returns values
        only, never the person they belong to.
        """
        rows = self._store.query(
            "SELECT value FROM knowledge_identifier_bindings WHERE status = 'active'"
            " UNION SELECT identifier FROM contact_identifiers ORDER BY 1"
        )
        return tuple(str(row[0]) for row in rows)

    def roster(
        self, *, context: TrustedReadContext, participant_ids: tuple[str, ...]
    ) -> tuple[tuple[str, tuple[str, ...]], ...]:
        """Names and released facts for proven members of one chat.

        Only people proven by an identifier binding are returned, and only names their
        visibility allows.  Facts come from permitted statements, never from a
        ``contact_fields`` copy.
        """
        checked = self._read_context(context)
        decision = self._retrieval.decide(checked)
        if not decision.allowed:
            return ()
        entries: list[tuple[str, tuple[str, ...]]] = []
        for raw in participant_ids:
            principal = str(raw or "").strip()
            if not principal:
                continue
            person_id = self.person_for_principal(principal)
            if person_id is None:
                continue
            name = self.display_name(person_id, context=checked, for_group=not checked.is_direct)
            if not name:
                continue
            profile = self.profile(person_id, context=checked)
            facts: list[str] = []
            for line in profile.context.text.splitlines():
                clean = line.strip()
                if clean:
                    facts.append(clean)
            if any(existing[0] == name for existing in entries):
                continue
            entries.append((name, tuple(facts)))
        return tuple(entries)

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
