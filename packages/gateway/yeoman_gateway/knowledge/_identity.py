"""Private identity engine: people, identifier bindings, names and merges.

Nothing here decides read permission, owner status or delivery targets.  Identity is a
naming and provenance problem; authorization stays with Policy and with
``knowledge._retrieval``.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Final

from yeoman_gateway.knowledge._store import KnowledgeStore
from yeoman_gateway.knowledge.models import (
    ALIAS_STATUSES,
    DEFAULT_NAMESPACE,
    GLOBAL_SCOPE_KEY,
    ChangeReceipt,
    EndpointResolution,
    Identifier,
    IdentifierBinding,
    KnowledgeError,
    MergeRedirect,
    NameObservation,
    PersonResolution,
    StatementCandidate,
    TrustedAdminContext,
    TrustedCaptureContext,
    TrustedIdentityObservation,
    TrustedReadContext,
    ValidationError,
    normalize_alias_value,
    validate_alias_kind,
    validate_name,
)

NAME_SOURCE_PRIORITY: Final[tuple[str, ...]] = ("owner_confirmed", "self_reported", "observed")

#: Identifier kinds whose values may be re-assigned by a platform (handles, usernames,
#: telegram ids of recycled accounts).  They never create a durable trusted identity.
_REASSIGNABLE_KINDS: Final[frozenset[str]] = frozenset(
    {"telegram_username", "handle", "email"}
)


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True, slots=True)
class PersonRow:
    person_id: str
    display_name: str
    revision: int
    status: str
    preferred_name: str | None
    preferred_name_source: str | None
    preferred_name_visibility: str


class IdentityEngine:
    """Person lookup, identifier binding, name priority and reversible merges."""

    def __init__(self, store: KnowledgeStore, *, authority: Any, policy: Any) -> None:
        self._store = store
        self._authority = authority
        self._policy = policy

    # ── reading ──────────────────────────────────────────────────────────────

    def get_person(self, person_id: str) -> PersonRow | None:
        row = self._store.query_one(
            "SELECT id, display_name, revision, status, preferred_name,"
            " preferred_name_source, preferred_name_visibility"
            " FROM contacts WHERE id = ?",
            (str(person_id),),
        )
        if row is None:
            return None
        return PersonRow(
            person_id=str(row["id"]),
            display_name=str(row["display_name"]),
            revision=int(row["revision"]),
            status=str(row["status"]),
            preferred_name=row["preferred_name"],
            preferred_name_source=row["preferred_name_source"],
            preferred_name_visibility=str(row["preferred_name_visibility"] or "public"),
        )

    def require_person(self, person_id: str) -> PersonRow:
        person = self.get_person(person_id)
        if person is None:
            raise KnowledgeError("unresolved", f"unknown person: {person_id}")
        return person

    def canonical_id(self, person_id: str, *, skip_ops: frozenset[str] = frozenset()) -> str:
        """Follow active merge redirects to the current canonical person id."""
        seen: set[str] = set()
        current = str(person_id)
        while True:
            if current in seen:
                raise KnowledgeError("identity_conflict", "redirect cycle detected")
            seen.add(current)
            rows = self._store.query(
                "SELECT operation_id, target_id FROM knowledge_identity_redirects"
                " WHERE source_id = ? AND active = 1 ORDER BY seq ASC, operation_id ASC",
                (current,),
            )
            nxt = next(
                (row for row in rows if str(row["operation_id"]) not in skip_ops), None
            )
            if nxt is None:
                return current
            current = str(nxt["target_id"])

    def canonical_ids(self, person_ids: tuple[str, ...] | list[str]) -> tuple[str, ...]:
        """Canonical people for a set of original ids, deduplicated, order preserved."""
        out: list[str] = []
        for person_id in person_ids:
            canonical = self.canonical_id(person_id)
            if canonical not in out:
                out.append(canonical)
        return tuple(out)

    def people_for_statement(
        self, statement_id: str, *, roles_only_active: bool = True
    ) -> tuple[tuple[str, str, str, int], ...]:
        """Raw role edges of a statement: (person_id, role, evidence_event, revision).

        A ``withheld`` role is history: it stays stored so the cutover is auditable, but
        it never names a person on a read path.
        """
        status_clause = " AND status = 'active'" if roles_only_active else ""
        rows = self._store.query(
            "SELECT person_id, role, evidence_source_id, evidence_revision"
            " FROM knowledge_statement_people WHERE statement_id = ?"
            f"{status_clause}"
            " ORDER BY person_id, role, evidence_source_id, evidence_revision",
            (str(statement_id),),
        )
        return tuple(
            (
                str(row["person_id"]),
                str(row["role"]),
                str(row["evidence_source_id"]),
                int(row["evidence_revision"]),
            )
            for row in rows
        )

    def person_ids_for_statements(
        self, statement_ids: tuple[str, ...] | list[str], roles: tuple[str, ...] = ()
    ) -> dict[str, tuple[str, ...]]:
        """Canonical people per statement, optionally filtered by role."""
        if not statement_ids:
            return {}
        placeholders = ",".join("?" for _ in statement_ids)
        sql = (
            "SELECT DISTINCT statement_id, person_id, role FROM knowledge_statement_people"
            f" WHERE statement_id IN ({placeholders}) AND status = 'active'"
        )
        params: list[Any] = [str(item) for item in statement_ids]
        if roles:
            role_placeholders = ",".join("?" for _ in roles)
            sql += f" AND role IN ({role_placeholders})"
            params.extend(roles)
        rows = self._store.query(sql, tuple(params))
        out: dict[str, list[str]] = {}
        for row in rows:
            canonical = self.canonical_id(str(row["person_id"]))
            bucket = out.setdefault(str(row["statement_id"]), [])
            if canonical not in bucket:
                bucket.append(canonical)
        return {key: tuple(value) for key, value in out.items()}

    def person_ids_for_query(
        self, person_ids: tuple[str, ...], roles: tuple[str, ...]
    ) -> tuple[str, ...]:
        """Canonical person ids that match the requested role filter.

        When roles are given, only people holding one of those roles are returned; the
        caller uses the result as an intersection filter over statements.
        """
        canonical = self.canonical_ids(person_ids)
        if not roles:
            return canonical
        placeholders = ",".join("?" for _ in canonical)
        role_placeholders = ",".join("?" for _ in roles)
        rows = self._store.query(
            "SELECT DISTINCT person_id FROM knowledge_statement_people"
            f" WHERE person_id IN ({placeholders}) AND role IN ({role_placeholders})"
            " AND status = 'active'",
            (*canonical, *roles),
        )
        allowed = {self.canonical_id(str(row["person_id"])) for row in rows}
        return tuple(item for item in canonical if item in allowed)

    # ── identifiers ──────────────────────────────────────────────────────────

    def binding_for(self, identifier: Identifier) -> IdentifierBinding | None:
        """The one *active* binding of a fully typed identifier, if any.

        Ended, conflicting and withheld rows are history: they are readable by id for
        audit, but they never resolve a current person or a delivery target.
        """
        row = self._store.query_one(
            "SELECT * FROM knowledge_identifier_bindings"
            " WHERE channel = ? AND kind = ? AND namespace = ? AND value = ?"
            " AND status = 'active' LIMIT 1",
            identifier.full_key,
        )
        if row is None:
            return None
        return self._row_to_binding(row)

    def binding_by_id(self, binding_id: str) -> IdentifierBinding | None:
        row = self._store.query_one(
            "SELECT * FROM knowledge_identifier_bindings WHERE binding_id = ?",
            (str(binding_id),),
        )
        return None if row is None else self._row_to_binding(row)

    def binding_at(self, identifier: Identifier, at_ms: int) -> IdentifierBinding | None:
        """A binding whose *proven* period contains ``at_ms``.

        An unknown start proves nothing about the past, so a historical lookup never
        falls back to "probably since forever".
        """
        rows = self._store.query(
            "SELECT * FROM knowledge_identifier_bindings"
            " WHERE channel = ? AND kind = ? AND namespace = ? AND value = ?"
            " ORDER BY valid_from_ms, binding_id",
            identifier.full_key,
        )
        matches = [
            binding
            for binding in (self._row_to_binding(row) for row in rows)
            if binding.covers(int(at_ms))
        ]
        if len(matches) != 1:
            return None
        return matches[0]

    def bindings_of(self, person_id: str) -> tuple[IdentifierBinding, ...]:
        rows = self._store.query(
            "SELECT * FROM knowledge_identifier_bindings WHERE person_id = ?"
            " ORDER BY channel, kind, value, valid_from_ms",
            (str(person_id),),
        )
        return tuple(self._row_to_binding(row) for row in rows)

    def active_bindings_of(self, person_id: str) -> tuple[IdentifierBinding, ...]:
        return tuple(item for item in self.bindings_of(person_id) if item.status == "active")

    @staticmethod
    def _row_to_binding(row: Any) -> IdentifierBinding:
        return IdentifierBinding(
            person_id=str(row["person_id"]),
            identifier=Identifier(
                channel=str(row["channel"]),
                kind=str(row["kind"]),
                value=str(row["value"]),
                namespace=str(row["namespace"] or DEFAULT_NAMESPACE),
            ),
            evidence_ref=str(row["evidence_ref"]),
            status=str(row["status"]),
            mapping_verified=bool(row["mapping_verified"]),
            created_ms=int(row["created_ms"]),
            updated_ms=int(row["updated_ms"]),
            binding_id=str(row["binding_id"]),
            valid_from_ms=int(row["valid_from_ms"] or 0),
            valid_until_ms=int(row["valid_until_ms"] or 0),
            observed_at_ms=int(row["observed_at_ms"] or 0),
            revision=int(row["revision"] or 1),
        )

    def _bind(
        self,
        *,
        person_id: str,
        identifier: Identifier,
        evidence_ref: str,
        mapping_verified: bool,
        status: str = "active",
        valid_from_ms: int | None = None,
        observed_at_ms: int = 0,
        binding_id: str = "",
    ) -> str:
        """Create or refresh one temporal binding.  Returns the binding id.

        A binding whose proven period is still open is refreshed in place (last seen
        wins); a caller that wants to end it must go through ``add_or_end_binding`` so
        the previous period survives as history instead of being overwritten.
        """
        ts = now_ms()
        from_ms = int(valid_from_ms or 0)
        if status == "withheld":
            raise ValidationError(
                "a withheld binding is recorded by record_unproven_identifier,"
                " not by the active binding writer"
            )
        existing = self._store.query_one(
            "SELECT binding_id, revision FROM knowledge_identifier_bindings"
            " WHERE channel = ? AND kind = ? AND namespace = ? AND value = ?"
            " AND status = 'active' LIMIT 1",
            identifier.full_key,
        )
        if existing is not None and binding_id and str(existing["binding_id"]) != str(binding_id):
            raise KnowledgeError("identity_conflict", "identifier is already actively bound")
        target_id = str(binding_id) or (
            str(existing["binding_id"]) if existing is not None else self._store.new_id()
        )
        revision = 1 if existing is None else int(existing["revision"]) + 1
        self._store.execute(
            """
            INSERT INTO knowledge_identifier_bindings
                (binding_id, channel, kind, namespace, value, person_id, status,
                 valid_from_ms, valid_until_ms, observed_at_ms, evidence_ref,
                 mapping_verified, revision, created_ms, updated_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(binding_id) DO UPDATE SET
                person_id = excluded.person_id,
                status = excluded.status,
                valid_from_ms = CASE
                    WHEN knowledge_identifier_bindings.valid_from_ms = 0
                    THEN excluded.valid_from_ms
                    ELSE knowledge_identifier_bindings.valid_from_ms END,
                observed_at_ms = MAX(
                    knowledge_identifier_bindings.observed_at_ms, excluded.observed_at_ms
                ),
                evidence_ref = excluded.evidence_ref,
                mapping_verified = excluded.mapping_verified,
                revision = excluded.revision,
                updated_ms = excluded.updated_ms
            """,
            (
                target_id,
                identifier.channel,
                identifier.kind,
                identifier.namespace,
                identifier.value,
                person_id,
                status,
                from_ms,
                int(observed_at_ms or ts),
                evidence_ref,
                int(mapping_verified),
                revision,
                ts,
                ts,
            ),
        )
        # Keep the legacy contact_identifiers cache in step for the migration window.
        # The claim is atomic: whoever inserts the row first owns the identifier, and a
        # loser never overwrites an existing owner.
        self._store.execute(
            "INSERT INTO contact_identifiers (channel, identifier, contact_id, kind)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(channel, identifier) DO NOTHING",
            (identifier.channel, identifier.value, person_id, identifier.kind),
        )
        return target_id

    def _create_stub(
        self,
        *,
        identifier: Identifier,
        evidence_ref: str,
        observed_name: str | None,
        mapping_verified: bool,
        observed_at_ms: int = 0,
    ) -> str:
        person_id = self._store.new_id()
        ts = now_ms()
        iso = _iso(ts)
        display = observed_name or identifier.value
        self._store.execute(
            "INSERT INTO contacts (id, display_name, phone_number, is_owner, created_at,"
            " updated_at, revision, status, preferred_name_visibility)"
            " VALUES (?, ?, NULL, 0, ?, ?, 1, 'active', 'public')"
            " ON CONFLICT(id) DO NOTHING",
            (person_id, display, iso, iso),
        )
        claimed = self._store.execute(
            "INSERT INTO contact_identifiers (channel, identifier, contact_id, kind)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(channel, identifier) DO NOTHING",
            (identifier.channel, identifier.value, person_id, identifier.kind),
        )
        if not claimed.rowcount:
            # Another observer already owns this identifier: drop this stub entirely
            # (the savepoint in resolve_observation rolls the contact row back too) and
            # let the caller read the winner.
            raise sqlite3.IntegrityError("identifier is already bound")
        self._bind(
            person_id=person_id,
            identifier=identifier,
            evidence_ref=evidence_ref,
            mapping_verified=mapping_verified,
            observed_at_ms=int(observed_at_ms or ts),
        )
        if observed_name:
            self._observe_name(
                person_id=person_id,
                name=observed_name,
                source="observed",
                observed_by="channel_adapter",
                ts=ts,
            )
        self._store.bump_identity_revision()
        return person_id

    def _observe_name(
        self,
        *,
        person_id: str,
        name: str,
        source: str,
        observed_by: str,
        ts: int,
        visibility: str = "public",
        alias_kind: str = "other_name",
        scope_key: str = GLOBAL_SCOPE_KEY,
        status: str = "observed",
        address_allowed: bool = False,
        supporting_statement_id: str | None = None,
        evidence_ref: str = "",
    ) -> None:
        """Record one observed name against the legacy compatibility row.

        Detection is not permission: an observed or confirmed alias is searchable but is
        only usable as an address once ``address_allowed`` was deliberately granted.
        """
        clean = validate_name(name)
        if status not in ALIAS_STATUSES:
            raise ValidationError(f"alias status must be one of {ALIAS_STATUSES}")
        normalized = normalize_alias_value(clean)
        scope = str(scope_key or "").strip() or GLOBAL_SCOPE_KEY
        iso = _iso(ts)
        self._store.execute(
            """
            INSERT INTO contact_aliases
                (contact_id, alias, source, first_seen, last_seen, visibility,
                 first_seen_ms, last_seen_ms, alias_kind, normalized_alias, scope_key,
                 status, address_allowed, is_preferred, supporting_statement_id,
                 evidence_ref, revision)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 1)
            ON CONFLICT(contact_id, alias, source) DO UPDATE SET
                last_seen = excluded.last_seen,
                last_seen_ms = excluded.last_seen_ms,
                normalized_alias = excluded.normalized_alias,
                alias_kind = excluded.alias_kind,
                scope_key = excluded.scope_key,
                status = CASE
                    WHEN contact_aliases.status = 'retired' THEN 'retired'
                    WHEN contact_aliases.status = 'confirmed' THEN 'confirmed'
                    ELSE excluded.status END,
                address_allowed = MAX(
                    contact_aliases.address_allowed, excluded.address_allowed
                ),
                supporting_statement_id = COALESCE(
                    contact_aliases.supporting_statement_id, excluded.supporting_statement_id
                ),
                evidence_ref = CASE
                    WHEN excluded.evidence_ref = '' THEN contact_aliases.evidence_ref
                    ELSE excluded.evidence_ref END,
                revision = contact_aliases.revision + 1
            """,
            (
                person_id,
                clean,
                source,
                iso,
                iso,
                visibility,
                ts,
                ts,
                alias_kind,
                normalized,
                scope,
                status,
                int(address_allowed),
                supporting_statement_id,
                evidence_ref,
            ),
        )
        if source == "observed":
            # An observed push name must never overwrite a confirmed preferred name.
            row = self._store.query_one(
                "SELECT preferred_name FROM contacts WHERE id = ?", (person_id,)
            )
            if row is not None and not row["preferred_name"]:
                self._store.execute(
                    "UPDATE contacts SET display_name = ?, updated_at = ? WHERE id = ?",
                    (clean, iso, person_id),
                )

    def aliases_of(self, person_id: str) -> tuple[NameObservation, ...]:
        rows = self._store.query(
            "SELECT * FROM contact_aliases WHERE contact_id = ? ORDER BY alias, source",
            (str(person_id),),
        )
        return tuple(self._row_to_alias(row) for row in rows)

    @staticmethod
    def _row_to_alias(row: Any) -> NameObservation:
        return NameObservation(
            person_id=str(row["contact_id"]),
            name=str(row["alias"]),
            source=str(row["source"]),
            visibility=str(row["visibility"] or "public"),
            first_seen_ms=int(row["first_seen_ms"] or 0),
            last_seen_ms=int(row["last_seen_ms"] or 0),
            alias_kind=str(row["alias_kind"] or "other_name"),
            normalized_alias=str(row["normalized_alias"] or ""),
            scope_key=str(row["scope_key"] or GLOBAL_SCOPE_KEY),
            status=str(row["status"] or "observed"),
            address_allowed=bool(row["address_allowed"]),
            is_preferred=bool(row["is_preferred"]),
            supporting_statement_id=row["supporting_statement_id"],
            evidence_ref=str(row["evidence_ref"] or ""),
            valid_until_ms=None if row["valid_until_ms"] is None else int(row["valid_until_ms"]),
            mapping_retracted=bool(row["mapping_retracted"]),
            revision=int(row["revision"] or 1),
        )

    def aliases_of_many(self, person_ids: tuple[str, ...]) -> dict[str, tuple[NameObservation, ...]]:
        if not person_ids:
            return {}
        placeholders = ",".join("?" for _ in person_ids)
        rows = self._store.query(
            "SELECT * FROM contact_aliases WHERE contact_id IN"
            f" ({placeholders}) ORDER BY alias, source",
            tuple(str(item) for item in person_ids),
        )
        out: dict[str, list[NameObservation]] = {}
        for row in rows:
            out.setdefault(str(row["contact_id"]), []).append(self._row_to_alias(row))
        return {key: tuple(value) for key, value in out.items()}

    # ── name priority ────────────────────────────────────────────────────────

    def merged_member_ids(self, person_id: str) -> tuple[str, ...]:
        """The original ids that currently merge into this person (including itself)."""
        canonical = self.canonical_id(person_id)
        members = [canonical]
        for redirect in self.active_redirects():
            if not redirect.active:
                continue
            if self.canonical_id(redirect.source_id) == canonical:
                if redirect.source_id not in members:
                    members.append(redirect.source_id)
        return tuple(members)

    def display_name(
        self,
        person_id: str,
        *,
        context: TrustedReadContext | None = None,
        for_group: bool = False,
    ) -> str | None:
        """Eligible address for a person, following the documented name priority.

        ``owner_confirmed`` beats ``self_reported`` beats the latest observed push
        name.  After a merge, the most recently confirmed name of the merged people
        wins deterministically - a merge never invents a new name and never deletes
        one.  A name restricted to private visibility is only used when the caller
        proved that the target is the person's direct conversation and no group is
        addressed.
        """
        canonical = self.canonical_id(person_id)
        person = self.get_person(canonical)
        if person is None:
            return None
        allow_private = bool(context is not None and context.is_direct and not for_group)

        confirmed: list[tuple[int, str]] = []
        for member in self.merged_member_ids(canonical):
            row = self.get_person(member)
            if row is None or not row.preferred_name:
                continue
            if row.preferred_name_visibility != "public" and not allow_private:
                continue
            set_ms = self._preferred_set_ms(member)
            confirmed.append((set_ms, row.preferred_name))
        if confirmed:
            confirmed.sort(key=lambda item: (item[0], item[1]))
            return confirmed[-1][1]

        aliases: list[NameObservation] = []
        for member in self.merged_member_ids(canonical):
            aliases.extend(self.aliases_of(member))
        for source in NAME_SOURCE_PRIORITY:
            candidates = [
                item
                for item in aliases
                if item.source == source and (item.visibility == "public" or allow_private)
            ]
            if candidates:
                candidates.sort(key=lambda item: (item.last_seen_ms, item.name))
                return candidates[-1].name

        if person.display_name:
            return person.display_name
        return None

    def _preferred_set_ms(self, person_id: str) -> int:
        row = self._store.query_one(
            "SELECT preferred_name_set_ms FROM contacts WHERE id = ?", (str(person_id),)
        )
        if row is None or row["preferred_name_set_ms"] is None:
            return 0
        return int(row["preferred_name_set_ms"])

    def neutral_address(self, identifier: Identifier | None) -> str | None:
        if identifier is None:
            return None
        return identifier.value

    # ── observation resolution ───────────────────────────────────────────────

    def resolve_observation(
        self,
        observation: TrustedIdentityObservation,
        *,
        context: TrustedReadContext | None = None,
        create_stub: bool = True,
    ) -> PersonResolution:
        """Resolve a verified platform observation to exactly one person.

        Ambiguity and conflicts are results, never a first-match merge.  Authoritative
        channel input may create a stub for a genuinely unknown identifier; model text
        never reaches this method.
        """
        evidence_ref = self._authority.verify_observation(observation)
        revision = self._store.identity_revision
        observed_at_ms = int(observation.observed_at_ms or now_ms())

        existing: dict[tuple[str, str, str, str], IdentifierBinding] = {}
        for identifier in observation.identifiers:
            binding = self.binding_for(identifier)
            if binding is not None and binding.status == "active":
                existing[identifier.full_key] = binding

        persons = {self.canonical_id(item.person_id) for item in existing.values()}
        if len(persons) > 1:
            return PersonResolution(
                status="conflict",
                person_id=None,
                display_name=None,
                identity_revision=revision,
                reason="identifiers_belong_to_different_people",
            )

        if persons:
            person_id = persons.pop()
            if observation.mapping_verified:
                for identifier in observation.identifiers:
                    if identifier.full_key in existing:
                        continue
                    if identifier.kind in _REASSIGNABLE_KINDS:
                        # A recyclable handle is not durable identity evidence.
                        continue
                    self._bind(
                        person_id=person_id,
                        identifier=identifier,
                        evidence_ref=evidence_ref,
                        mapping_verified=True,
                        observed_at_ms=observed_at_ms,
                    )
            elif len(observation.identifiers) > 1:
                return PersonResolution(
                    status="ambiguous",
                    person_id=None,
                    display_name=None,
                    identity_revision=revision,
                    reason="unverified_multi_identifier_mapping",
                )
            if observation.observed_name:
                self._observe_name(
                    person_id=person_id,
                    name=observation.observed_name,
                    source="observed",
                    observed_by="channel_adapter",
                    ts=observed_at_ms,
                )
            return PersonResolution(
                status="resolved",
                person_id=person_id,
                display_name=self.display_name(person_id, context=context),
                identity_revision=self._store.identity_revision,
                reason="existing_binding",
            )

        # A bare identifier of a reassignable kind is not enough to mint a person.
        primary = sorted(observation.identifiers, key=lambda item: item.key)[0]
        durable = [item for item in observation.identifiers if item.kind not in _REASSIGNABLE_KINDS]
        if not durable:
            return PersonResolution(
                status="unresolved",
                person_id=None,
                display_name=None,
                identity_revision=revision,
                reason="identifier_kind_is_not_durable_identity_evidence",
            )
        if not create_stub:
            return PersonResolution(
                status="unresolved",
                person_id=None,
                display_name=None,
                identity_revision=revision,
                reason="no_stub_creation_allowed",
            )
        try:
            with self._store.savepoint():
                person_id = self._create_stub(
                    identifier=primary,
                    evidence_ref=evidence_ref,
                    observed_name=observation.observed_name,
                    mapping_verified=observation.mapping_verified,
                    observed_at_ms=observed_at_ms,
                )
        except sqlite3.IntegrityError:
            # A concurrent observer won the identifier: read the winner instead of
            # creating a second person for the same platform account.
            winner = self.binding_for(primary)
            if winner is None or winner.status != "active":  # pragma: no cover - defensive
                raise
            person_id = self.canonical_id(winner.person_id)
            return PersonResolution(
                status="resolved",
                person_id=person_id,
                display_name=self.display_name(person_id, context=context),
                identity_revision=self._store.identity_revision,
                reason="lost_binding_race",
            )
        if observation.mapping_verified:
            for identifier in observation.identifiers:
                if identifier.full_key == primary.full_key:
                    continue
                if identifier.kind in _REASSIGNABLE_KINDS:
                    continue
                self._bind(
                    person_id=person_id,
                    identifier=identifier,
                    evidence_ref=evidence_ref,
                    mapping_verified=True,
                    observed_at_ms=observed_at_ms,
                )
        return PersonResolution(
            status="resolved",
            person_id=person_id,
            display_name=self.display_name(person_id, context=context),
            identity_revision=self._store.identity_revision,
            reason="created_stub_from_verified_identifier",
        )

    # ── names and aliases ────────────────────────────────────────────────────

    def alias_by_id(self, alias_id: int) -> NameObservation | None:
        row = self._store.query_one(
            "SELECT * FROM contact_aliases WHERE id = ?", (int(alias_id),)
        )
        return None if row is None else self._row_to_alias(row)

    def observe_alias(
        self,
        *,
        person_id: str,
        name: str,
        alias_kind: str,
        scope_key: str,
        evidence_ref: str,
        status: str = "observed",
        address_allowed: bool = False,
        supporting_statement_id: str | None = None,
        visibility: str = "public",
        valid_until_ms: int | None = None,
        source: str = "observed",
    ) -> NameObservation:
        """Record one alias with an explicit kind, context and evidence.

        Recognition is not permission.  An alias produced by a platform observation or an
        extractor is searchable, but it is only usable as an address once
        ``address_allowed`` was granted deliberately - and a scope key is required, because
        a name released in one chat is not a global fact.
        """
        canonical = self.canonical_id(person_id)
        self.require_person(canonical)
        clean = validate_name(name)
        kind = validate_alias_kind(alias_kind)
        if status not in ALIAS_STATUSES:
            raise ValidationError(f"alias status must be one of {ALIAS_STATUSES}")
        scope = str(scope_key or "").strip()
        if not scope:
            raise ValidationError("an alias needs an explicit scope key")
        if visibility not in ("public", "private"):
            raise ValidationError("alias visibility must be public or private")
        evidence = str(evidence_ref or "").strip()
        if status in ("confirmed", "candidate") and not evidence and not supporting_statement_id:
            raise ValidationError("a confirmed or candidate alias needs a supporting evidence")
        ts = now_ms()
        self._observe_name(
            person_id=canonical,
            name=clean,
            source=str(source),
            observed_by="alias_writer",
            ts=ts,
            visibility=visibility,
            alias_kind=kind,
            scope_key=scope,
            status=status,
            address_allowed=address_allowed,
            supporting_statement_id=supporting_statement_id,
            evidence_ref=evidence,
        )
        if valid_until_ms is not None:
            self._store.execute(
                "UPDATE contact_aliases SET valid_until_ms = ?"
                " WHERE contact_id = ? AND alias = ? AND source = ?",
                (int(valid_until_ms), canonical, clean, str(source)),
            )
        row = self._store.query_one(
            "SELECT * FROM contact_aliases WHERE contact_id = ? AND alias = ? AND source = ?",
            (canonical, clean, str(source)),
        )
        assert row is not None  # the insert above guarantees the row
        return self._row_to_alias(row)

    def set_alias_preference(
        self,
        *,
        alias_id: int,
        expected_revision: int,
        context: TrustedAdminContext,
        address_allowed: bool = True,
    ) -> ChangeReceipt:
        """Make one alias the preferred address of its person in its own context.

        At most one preferred, address-allowed alias per person and scope: the previous
        holder of that slot is demoted in the same transaction, so a reader never has to
        choose between two "preferred" names.
        """
        self._require_owner(context)
        self._policy.require_admin(context)
        alias = self.alias_by_id(alias_id)
        if alias is None:
            raise KnowledgeError("unresolved", "unknown alias")
        if alias.status not in ("confirmed", "observed"):
            raise KnowledgeError("identity_conflict", "a retired alias cannot be preferred")
        canonical = self.canonical_id(alias.person_id)
        self._store.execute(
            "UPDATE contact_aliases SET is_preferred = 0, revision = revision + 1"
            " WHERE contact_id = ? AND scope_key = ? AND is_preferred = 1",
            (canonical, alias.scope_key),
        )
        self._store.execute(
            "UPDATE contact_aliases SET is_preferred = 1, address_allowed = ?,"
            " revision = revision + 1 WHERE id = ?",
            (int(bool(address_allowed)), int(alias_id)),
        )
        operation_id = self._record_operation(
            kind="alias_preference",
            actor=context.actor_principal,
            authorization_ref=context.authorization_ref,
            payload={
                "alias_id": int(alias_id),
                "person_id": canonical,
                "scope_key": alias.scope_key,
                "expected_revision": int(expected_revision),
            },
        )
        revision = self._store.bump_identity_revision()
        return ChangeReceipt(
            operation_id=operation_id,
            identity_revision=revision,
            acl_epoch=self._store.acl_epoch,
            changed_ids=(canonical,),
        )

    def retire_alias(
        self,
        *,
        alias_id: int,
        expected_revision: int,
        context: TrustedAdminContext,
        reason: str = "not_wanted",
        correct_mapping: bool = False,
    ) -> ChangeReceipt:
        """Stop using a name as an address, or retract the mapping behind it.

        ``reason="not_wanted"`` ("bitte nicht mehr so nennen") only withdraws the
        *addressing* permission: the name stays searchable inside its existing rights, so
        old messages remain findable.  ``correct_mapping=True`` ("so habe ich nie
        geheissen") retracts the association itself, which also removes it from search.
        """
        self._require_owner(context)
        self._policy.require_admin(context)
        alias = self.alias_by_id(alias_id)
        if alias is None:
            raise KnowledgeError("unresolved", "unknown alias")
        canonical = self.canonical_id(alias.person_id)
        ts = now_ms()
        self._store.execute(
            "UPDATE contact_aliases SET status = 'retired', address_allowed = 0,"
            " is_preferred = 0, valid_until_ms = COALESCE(valid_until_ms, ?),"
            " mapping_retracted = CASE WHEN ? = 1 THEN 1 ELSE mapping_retracted END,"
            " revision = revision + 1 WHERE id = ?",
            (ts, int(bool(correct_mapping)), int(alias_id)),
        )
        operation_id = self._record_operation(
            kind="alias_retire",
            actor=context.actor_principal,
            authorization_ref=context.authorization_ref,
            payload={
                "alias_id": int(alias_id),
                "person_id": canonical,
                "reason": str(reason),
                "correct_mapping": bool(correct_mapping),
                "expected_revision": int(expected_revision),
            },
        )
        revision = self._store.bump_identity_revision()
        return ChangeReceipt(
            operation_id=operation_id,
            identity_revision=revision,
            acl_epoch=self._store.acl_epoch,
            changed_ids=(canonical,),
        )

    def searchable_aliases_of(self, person_id: str) -> tuple[NameObservation, ...]:
        """Aliases that stay findable: everything except a retracted mapping."""
        canonical = self.canonical_id(person_id)
        return tuple(item for item in self.aliases_of(canonical) if item.findable)

    def address_aliases_of(
        self, person_id: str, *, scope_key: str | None = None
    ) -> tuple[NameObservation, ...]:
        """Aliases that may be used to *address* the person in a context."""
        canonical = self.canonical_id(person_id)
        out: list[NameObservation] = []
        for item in self.aliases_of(canonical):
            if not item.usable_as_address:
                continue
            if scope_key is not None and item.scope_key not in (scope_key, GLOBAL_SCOPE_KEY):
                continue
            out.append(item)
        return tuple(sorted(out, key=lambda item: (not item.is_preferred, item.name)))

    def delivery_identifiers_for_alias(
        self,
        alias: str,
        *,
        channel: str,
        scope_key: str | None = None,
    ) -> tuple[Identifier, ...]:
        """Proven delivery identifiers of the people carrying this exact alias.

        Recognising a name is not permission to address somebody with it (spec 7.3), so
        an alias counts here only while it is ``observed``/``confirmed``, explicitly
        ``address_allowed``, neither retracted nor withdrawn, and - when a context is
        given - inside that context or the explicitly released global one.

        Every candidate is returned.  Ambiguity stays visible: the caller decides
        "exactly one or nothing" and a first match is never a resolution.
        """
        query = str(alias or "").strip()
        if not query:
            return ()
        channel_key = str(channel or "").strip().lower()
        if not channel_key:
            return ()
        sql = (
            "SELECT DISTINCT a.contact_id AS person_id FROM contact_aliases a"
            " JOIN contacts c ON c.id = a.contact_id"
            " WHERE a.alias = ? COLLATE NOCASE"
            " AND a.status IN ('observed','confirmed')"
            " AND a.address_allowed = 1"
            " AND a.mapping_retracted = 0"
            " AND a.valid_until_ms IS NULL"
            " AND c.status = 'active'"
        )
        params: list[object] = [query]
        if scope_key is not None:
            sql += " AND (a.scope_key = ? OR a.scope_key = ?)"
            params.extend([str(scope_key), GLOBAL_SCOPE_KEY])
        people = [
            self.canonical_id(str(row["person_id"]))
            for row in self._store.query(sql, tuple(params))
        ]
        identifiers: list[Identifier] = []
        seen: set[tuple[str, str, str, str]] = set()
        for person_id in dict.fromkeys(people):
            for binding in self.active_bindings_of(person_id):
                identifier = binding.identifier
                if identifier.channel != channel_key or identifier.full_key in seen:
                    continue
                seen.add(identifier.full_key)
                identifiers.append(identifier)
        return tuple(sorted(identifiers, key=lambda item: (item.kind, item.value)))

    # ── endpoint resolution by identifier ────────────────────────────────────

    def resolve_identifier(
        self, identifier: Identifier, *, at_ms: int | None = None
    ) -> EndpointResolution:
        """Resolve one fully typed identifier, optionally at a proven past instant.

        ``withheld``, ``ended`` and ``conflict`` rows are never an answer: they are the
        audit trail of a claim that did not earn person authority.  Several active
        owners is ``conflict``, never the first row.
        """
        revision = self._store.identity_revision
        if at_ms is None:
            rows = self._store.query(
                "SELECT * FROM knowledge_identifier_bindings"
                " WHERE channel = ? AND kind = ? AND namespace = ? AND value = ?"
                " AND status = 'active'",
                identifier.full_key,
            )
            bindings = [self._row_to_binding(row) for row in rows]
        else:
            binding = self.binding_at(identifier, int(at_ms))
            bindings = [] if binding is None else [binding]
        if not bindings:
            return EndpointResolution(
                status="unresolved",
                person_id=None,
                identifier=None,
                identity_revision=revision,
                reason="no_proven_binding_for_identifier",
            )
        owners = {self.canonical_id(item.person_id) for item in bindings}
        if len(owners) > 1:
            return EndpointResolution(
                status="conflict",
                person_id=None,
                identifier=None,
                identity_revision=revision,
                reason="identifier_is_claimed_by_several_people",
            )
        person_id = owners.pop()
        if self.get_person(person_id) is None:
            return EndpointResolution(
                status="unresolved",
                person_id=person_id,
                identifier=None,
                identity_revision=revision,
                reason="binding_points_at_an_unknown_person",
            )
        return EndpointResolution(
            status="resolved",
            person_id=person_id,
            identifier=bindings[0].identifier,
            identity_revision=revision,
            reason="proven_active_binding" if at_ms is None else "proven_historic_binding",
        )

    # ── legacy compatibility projection ──────────────────────────────────────

    def record_legacy_projection(
        self, *, person_id: str, identifier: Identifier, evidence_ref: str
    ) -> None:
        """Write only the compatibility projection, never a person authority.

        ``contact_identifiers`` is explicitly not an authority.  The legacy co-existence
        path may populate it so existing name/identifier searches keep working, but it
        must not mint an active v2 binding: an unproven import stays ``withheld`` until a
        channel adapter or an audited admin operation proves it.
        """
        self._store.execute(
            "INSERT INTO contact_identifiers (channel, identifier, contact_id, kind)"
            " VALUES (?, ?, ?, ?) ON CONFLICT(channel, identifier) DO NOTHING",
            (identifier.channel, identifier.value, person_id, identifier.kind),
        )

    def record_unproven_identifier(
        self, *, person_id: str, identifier: Identifier, evidence_ref: str
    ) -> str:
        """Record an unproven candidate binding, auditable but not person-effective.

        Deliberately never touches an identifier that some other person already holds
        with an *active* proven binding: a legacy import must not be able to shadow or
        steal a live identity.  The candidate row is still written, so the case stays
        visible in the cutover ledger.
        """
        ts = now_ms()
        binding_id = self._store.new_id()
        taken = self._store.query_one(
            "SELECT person_id FROM knowledge_identifier_bindings"
            " WHERE channel = ? AND kind = ? AND namespace = ? AND value = ?"
            " AND status = 'active' LIMIT 1",
            identifier.full_key,
        )
        if taken is not None and self.canonical_id(str(taken["person_id"])) != self.canonical_id(
            person_id
        ):
            self.record_legacy_projection(
                person_id=person_id, identifier=identifier, evidence_ref=evidence_ref
            )
            return ""
        self._store.execute(
            "INSERT INTO knowledge_identifier_bindings (binding_id, channel, kind,"
            " namespace, value, person_id, status, valid_from_ms, valid_until_ms,"
            " observed_at_ms, evidence_ref, mapping_verified, revision, created_ms,"
            " updated_ms) VALUES (?, ?, ?, ?, ?, ?, 'withheld', 0, 0, ?, ?, 0, 1, ?, ?)"
            " ON CONFLICT DO NOTHING",
            (
                binding_id,
                identifier.channel,
                identifier.kind,
                identifier.namespace,
                identifier.value,
                person_id,
                ts,
                evidence_ref,
                ts,
                ts,
            ),
        )
        self.record_legacy_projection(
            person_id=person_id, identifier=identifier, evidence_ref=evidence_ref
        )
        return binding_id

    # ── lookup by name ───────────────────────────────────────────────────────
    def search_by_name(
        self,
        name: str,
        *,
        context: TrustedReadContext | None = None,
        limit: int = 25,
    ) -> tuple[PersonResolution, ...]:
        """Every person whose eligible names match.  Same name may yield several people.

        Group chats never see names that are only privately released.
        """
        if not isinstance(name, str) or not name.strip():
            raise ValidationError("search name must not be empty")
        pattern = f"%{name.strip()}%"
        rows = self._store.query(
            """
            SELECT DISTINCT c.id AS id FROM contacts c
             LEFT JOIN contact_aliases a ON a.contact_id = c.id
             WHERE c.status = 'active'
               AND (c.display_name LIKE ? COLLATE NOCASE
                    OR (c.preferred_name IS NOT NULL
                        AND c.preferred_name LIKE ? COLLATE NOCASE)
                    OR a.alias LIKE ? COLLATE NOCASE)
             ORDER BY c.id
             LIMIT ?
            """,
            (pattern, pattern, pattern, int(max(1, min(limit, 50)))),
        )
        results: list[PersonResolution] = []
        for row in rows:
            person_id = self.canonical_id(str(row["id"]))
            if any(item.person_id == person_id for item in results):
                continue
            results.append(
                PersonResolution(
                    status="resolved",
                    person_id=person_id,
                    display_name=self.display_name(person_id, context=context),
                    identity_revision=self._store.identity_revision,
                    reason="name_match",
                )
            )
        return tuple(results)

    def eligible_people_for_context(
        self, context: TrustedReadContext, *, limit: int = 200
    ) -> tuple[tuple[str, str | None], ...]:
        """People that may be named inside a chat: proven chat members, canonicalised."""
        if not context.membership_known:
            return ()
        members = context.recipient_principals or frozenset()
        if context.principal_id not in members:
            return ()
        out: list[tuple[str, str | None]] = []
        for principal in sorted(members):
            person_id = self.person_id_for_principal(principal)
            if person_id is None:
                continue
            canonical = self.canonical_id(person_id)
            if any(item[0] == canonical for item in out):
                continue
            out.append((canonical, self.display_name(canonical, context=context)))
            if len(out) >= limit:
                break
        return tuple(out)

    def person_id_for_principal(self, principal: str) -> str | None:
        """Map a security principal to a person *through proven active bindings*.

        Real principals are channel-qualified (``whatsapp:491...``); the mapping is a
        lookup, never a guess from a display name.  The compatibility projection
        ``contact_identifiers`` is deliberately *not* consulted: an unproven legacy row
        must not resolve a person on the operational path.
        """
        token = str(principal or "").strip()
        if not token:
            return None
        channel, _, value = token.partition(":")
        candidates: list[tuple[str, str]] = []
        if value:
            candidates.append((channel, value))
            if channel == "whatsapp":
                candidates.append(("whatsapp", f"{value}@s.whatsapp.net"))
                candidates.append(("whatsapp", f"{value}@lid"))
        else:
            candidates.append(("whatsapp", token))
            candidates.append(("whatsapp", f"{token}@s.whatsapp.net"))
            candidates.append(("whatsapp", f"{token}@lid"))
        for candidate_channel, candidate_value in candidates:
            rows = self._store.query(
                "SELECT person_id FROM knowledge_identifier_bindings"
                " WHERE channel = ? AND value = ? AND status = 'active'",
                (candidate_channel, candidate_value),
            )
            owners = {self.canonical_id(str(row["person_id"])) for row in rows}
            if len(owners) > 1:
                # Several proven identities claim this principal: that is a conflict,
                # not a licence to pick the first row.
                return None
            if owners:
                return owners.pop()
        return None

    def resolve_endpoint(
        self,
        person_id: str,
        channel: str,
        *,
        prefer_kind: str | None = None,
        at_ms: int | None = None,
    ) -> EndpointResolution:
        """One proven endpoint - or an explicit error, never a random match.

        Phone and LID have no built-in order: a caller that wants the phone JID (or the
        LID) of a person says so with ``prefer_kind``.  Without a preference, two kinds
        on the same channel stay ``ambiguous`` instead of silently picking one.

        With ``at_ms`` the lookup is historical: only bindings whose proven period
        contains that instant are candidates, and an unknown start never covers it.
        """
        canonical = self.canonical_id(person_id)
        revision = self._store.identity_revision
        if self.get_person(canonical) is None:
            return EndpointResolution(
                status="unresolved",
                person_id=None,
                identifier=None,
                identity_revision=revision,
                reason="unknown_person",
            )
        channel_key = str(channel).strip().lower()
        rows = self._store.query(
            "SELECT * FROM knowledge_identifier_bindings"
            " WHERE person_id = ? AND channel = ?"
            " ORDER BY kind, value, valid_from_ms",
            (canonical, channel_key),
        )
        bindings = [self._row_to_binding(row) for row in rows]
        if at_ms is None:
            bindings = [item for item in bindings if item.status == "active"]
        else:
            bindings = [item for item in bindings if item.covers(int(at_ms))]
        if not bindings:
            return EndpointResolution(
                status="unresolved",
                person_id=canonical,
                identifier=None,
                identity_revision=revision,
                reason="no_verified_endpoint_for_channel",
            )
        if len(bindings) > 1 and len({item.identifier.kind for item in bindings}) > 1:
            if prefer_kind:
                preferred = [item for item in bindings if item.identifier.kind == prefer_kind]
                if len(preferred) == 1:
                    return EndpointResolution(
                        status="resolved",
                        person_id=canonical,
                        identifier=preferred[0].identifier,
                        identity_revision=revision,
                        reason="explicit_kind_preference",
                    )
            return EndpointResolution(
                status="ambiguous",
                person_id=canonical,
                identifier=None,
                identity_revision=revision,
                reason="multiple_endpoint_kinds_require_explicit_choice",
            )
        return EndpointResolution(
            status="resolved",
            person_id=canonical,
            identifier=bindings[0].identifier,
            identity_revision=revision,
            reason="single_verified_endpoint",
        )

    # ── mutations ────────────────────────────────────────────────────────────

    def set_preferred_name(
        self,
        person_id: str,
        name: str,
        *,
        context: TrustedAdminContext,
        source: str = "owner_confirmed",
        visibility: str = "public",
        observed_name: str | None = None,
        evidence_ref: str = "",
    ) -> ChangeReceipt:
        """Set the released address preference.  This changes no rights at all."""
        self._require_owner(context)
        self._policy.require_admin(context)
        canonical = self.canonical_id(person_id)
        self.require_person(canonical)
        clean = validate_name(name)
        if source not in ("owner_confirmed", "self_reported"):
            raise ValidationError("preferred name source must be owner_confirmed or self_reported")
        if visibility not in ("public", "private"):
            raise ValidationError("preferred name visibility must be public or private")
        ts = now_ms()
        iso = _iso(ts)
        self._store.execute(
            "UPDATE contacts SET preferred_name = ?, preferred_name_source = ?,"
            " preferred_name_visibility = ?, preferred_name_set_ms = ?, preferred_name_set_by = ?,"
            " display_name = ?, updated_at = ?, revision = revision + 1 WHERE id = ?",
            (clean, source, visibility, ts, context.actor_principal, clean, iso, canonical),
        )
        self._observe_name(
            person_id=canonical,
            name=clean,
            source=source,
            observed_by=context.actor_principal,
            ts=ts,
            visibility=visibility,
        )
        if observed_name:
            self._observe_name(
                person_id=canonical,
                name=observed_name,
                source="observed",
                observed_by="channel_adapter",
                ts=ts,
            )
        operation_id = self._record_operation(
            kind="preferred_name",
            actor=context.actor_principal,
            authorization_ref=context.authorization_ref,
            payload={"person_id": canonical, "source": source, "visibility": visibility},
        )
        revision = self._store.bump_identity_revision()
        return ChangeReceipt(
            operation_id=operation_id,
            identity_revision=revision,
            acl_epoch=self._store.acl_epoch,
            changed_ids=(canonical,),
        )

    def _require_owner(self, context: TrustedAdminContext) -> None:
        """Owner authority is a Policy decision; the DTO only carries its result."""
        if not context.owner:
            raise KnowledgeError("unauthorized", "admin context lacks owner authority")

    def add_binding(
        self,
        *,
        person_id: str,
        identifier: Identifier,
        evidence_ref: str,
        mapping_verified: bool,
        context: TrustedAdminContext,
    ) -> ChangeReceipt:
        self._require_owner(context)
        self._policy.require_admin(context)
        canonical = self.canonical_id(person_id)
        self.require_person(canonical)
        verified = self._authority.verify_evidence_ref(evidence_ref)
        existing = self.binding_for(identifier)
        if existing is not None and self.canonical_id(existing.person_id) != canonical:
            raise KnowledgeError(
                "identity_conflict",
                "identifier is already bound to another person",
            )
        self._bind(
            person_id=canonical,
            identifier=identifier,
            evidence_ref=verified,
            mapping_verified=mapping_verified,
        )
        operation_id = self._record_operation(
            kind="binding",
            actor=context.actor_principal,
            authorization_ref=context.authorization_ref,
            payload={"person_id": canonical, "identifier": list(identifier.key)},
        )
        revision = self._store.bump_identity_revision()
        return ChangeReceipt(
            operation_id=operation_id,
            identity_revision=revision,
            acl_epoch=self._store.acl_epoch,
            changed_ids=(canonical,),
        )

    def add_or_end_binding(
        self,
        *,
        person_id: str,
        identifier: Identifier,
        evidence_ref: str,
        mapping_verified: bool = True,
        context: TrustedAdminContext,
        expected_revision: int,
        valid_from_ms: int | None = None,
        end_binding_id: str | None = None,
        end_at_ms: int | None = None,
        change_kind: str = "binding",
    ) -> ChangeReceipt:
        """The one audited binding-maintenance operation.

        Three cases share one serialized transaction, because they are the same
        question - who owns this identifier, from when to when:

        * **claim**: create an active binding for a person;
        * **extend**: refresh the evidence of a binding this person already holds;
        * **hand over**: end ``end_binding_id`` at ``end_at_ms`` and create the new
          active binding for ``person_id``.  The old row survives as history, so a
          message from before the hand-over still resolves to the earlier person.

        ``expected_revision`` is a compare-and-swap on the identity revision: an operator
        acting on stale knowledge changes nothing.  An overlapping proven period for two
        different people is refused rather than silently preferred.
        """
        self._require_owner(context)
        self._policy.require_admin(context)
        if int(expected_revision) != self._store.identity_revision:
            raise KnowledgeError("stale_revision", "identity revision changed")
        canonical = self.canonical_id(person_id)
        self.require_person(canonical)
        verified = self._authority.verify_evidence_ref(evidence_ref)
        ts = now_ms()
        changed: list[str] = [canonical]

        if end_binding_id:
            previous = self.binding_by_id(end_binding_id)
            if previous is None:
                raise KnowledgeError("unresolved", "unknown binding to end")
            if previous.status != "active":
                raise KnowledgeError("identity_conflict", "binding is not active")
            if previous.identifier.full_key != identifier.full_key:
                raise KnowledgeError(
                    "invalid_input", "the ended binding must be the same identifier"
                )
            end_ms = int(end_at_ms or ts)
            if end_ms <= 0:
                raise ValidationError("end_at_ms must be positive")
            if previous.valid_from_ms and end_ms <= previous.valid_from_ms:
                raise ValidationError("a binding cannot end before it starts")
            self._store.execute(
                "UPDATE knowledge_identifier_bindings SET status = 'ended',"
                " valid_until_ms = ?, revision = revision + 1, updated_ms = ?"
                " WHERE binding_id = ? AND status = 'active'",
                (end_ms, ts, str(end_binding_id)),
            )
            changed.append(self.canonical_id(previous.person_id))

        existing = self.binding_for(identifier)
        if existing is not None and self.canonical_id(existing.person_id) != canonical:
            # There is still an active binding for the identifier after the hand-over (or
            # there never was a hand-over): refuse instead of stealing it.
            raise KnowledgeError(
                "identity_conflict",
                "identifier is already actively bound to another person",
            )
        self._assert_no_period_overlap(
            identifier=identifier,
            person_id=canonical,
            valid_from_ms=valid_from_ms,
            exclude_binding_id=end_binding_id,
        )
        binding_id = self._bind(
            person_id=canonical,
            identifier=identifier,
            evidence_ref=verified,
            mapping_verified=mapping_verified,
            valid_from_ms=valid_from_ms,
            observed_at_ms=ts,
        )
        operation_id = self._record_operation(
            kind=change_kind,
            actor=context.actor_principal,
            authorization_ref=context.authorization_ref,
            payload={
                "person_id": canonical,
                "identifier": list(identifier.full_key),
                "binding_id": binding_id,
                "ended_binding_id": end_binding_id or "",
                "evidence_ref": verified,
            },
        )
        revision = self._store.bump_identity_revision()
        return ChangeReceipt(
            operation_id=operation_id,
            identity_revision=revision,
            acl_epoch=self._store.acl_epoch,
            changed_ids=tuple(dict.fromkeys(changed)),
        )

    def end_binding(
        self,
        *,
        binding_id: str,
        expected_revision: int,
        context: TrustedAdminContext,
        end_at_ms: int | None = None,
        reason: str = "reassigned",
    ) -> ChangeReceipt:
        """End exactly one binding and keep its proven period as history."""
        self._require_owner(context)
        self._policy.require_admin(context)
        if int(expected_revision) != self._store.identity_revision:
            raise KnowledgeError("stale_revision", "identity revision changed")
        previous = self.binding_by_id(binding_id)
        if previous is None:
            raise KnowledgeError("unresolved", "unknown binding")
        if previous.status != "active":
            raise KnowledgeError("identity_conflict", "binding is not active")
        ts = now_ms()
        end_ms = int(end_at_ms or ts)
        if previous.valid_from_ms and end_ms <= previous.valid_from_ms:
            raise ValidationError("a binding cannot end before it starts")
        self._store.execute(
            "UPDATE knowledge_identifier_bindings SET status = 'ended', valid_until_ms = ?,"
            " revision = revision + 1, updated_ms = ?"
            " WHERE binding_id = ? AND status = 'active'",
            (end_ms, ts, str(binding_id)),
        )
        operation_id = self._record_operation(
            kind="binding_end",
            actor=context.actor_principal,
            authorization_ref=context.authorization_ref,
            payload={"binding_id": str(binding_id), "reason": str(reason), "end_ms": end_ms},
        )
        revision = self._store.bump_identity_revision()
        return ChangeReceipt(
            operation_id=operation_id,
            identity_revision=revision,
            acl_epoch=self._store.acl_epoch,
            changed_ids=(self.canonical_id(previous.person_id),),
        )

    def _assert_no_period_overlap(
        self,
        *,
        identifier: Identifier,
        person_id: str,
        valid_from_ms: int | None,
        exclude_binding_id: str | None,
    ) -> None:
        """Two *proven* periods of different people may not overlap on one identifier.

        Only bindings with a known start participate: an unknown start is knowledge time,
        not a claim about the past, and blocking on it would make the check unusable
        without adding any safety.
        """
        start = int(valid_from_ms or 0)
        if start <= 0:
            # The new claim has no proven start, so there is no period to compare.  The
            # partial unique index still guarantees at most one *active* binding.
            return
        rows = self._store.query(
            "SELECT binding_id, person_id, valid_from_ms, valid_until_ms"
            " FROM knowledge_identifier_bindings"
            " WHERE channel = ? AND kind = ? AND namespace = ? AND value = ?",
            identifier.full_key,
        )
        for row in rows:
            other_id = str(row["binding_id"])
            if exclude_binding_id and other_id == str(exclude_binding_id):
                continue
            if self.canonical_id(str(row["person_id"])) == person_id:
                continue
            other_start = int(row["valid_from_ms"] or 0)
            if other_start <= 0:
                # The other claim has no proven start either: nothing comparable.
                continue
            other_end = int(row["valid_until_ms"] or 0)
            if other_end and other_end <= start:
                continue  # the other proven period is entirely before the new one
            raise KnowledgeError(
                "identity_conflict",
                "the proven periods of two people overlap on this identifier",
            )

    def _record_operation(
        self,
        *,
        kind: str,
        actor: str,
        authorization_ref: str,
        payload: dict[str, Any],
    ) -> str:
        operation_id = self._store.new_id()
        self._store.execute(
            "INSERT INTO knowledge_identity_ops (operation_id, kind, actor_principal,"
            " authorization_ref, payload_json, created_ms, undone)"
            " VALUES (?, ?, ?, ?, ?, ?, 0)",
            (
                operation_id,
                kind,
                actor,
                authorization_ref,
                _json(payload),
                now_ms(),
            ),
        )
        return operation_id

    # ── merge / undo ─────────────────────────────────────────────────────────

    def active_redirects(self) -> tuple[MergeRedirect, ...]:
        rows = self._store.query(
            "SELECT * FROM knowledge_identity_redirects ORDER BY seq, operation_id"
        )
        return tuple(
            MergeRedirect(
                operation_id=str(row["operation_id"]),
                source_id=str(row["source_id"]),
                target_id=str(row["target_id"]),
                actor_principal=str(row["actor_principal"]),
                authorization_ref=str(row["authorization_ref"]),
                created_ms=int(row["created_ms"]),
                active=bool(row["active"]),
                undone_ms=None if row["undone_ms"] is None else int(row["undone_ms"]),
            )
            for row in rows
        )

    def merge_people(
        self,
        target_id: str,
        source_id: str,
        *,
        expected_revision: int,
        context: TrustedAdminContext,
    ) -> ChangeReceipt:
        """Record ``source_id -> target_id`` as a reversible redirect.

        Both person rows survive; identifier bindings and statement edges keep their
        original person ids and are only canonicalised on read.  No principal, quota,
        audience or owner flag is touched.
        """
        self._require_owner(context)
        self._policy.require_admin(context)
        if int(expected_revision) != self._store.identity_revision:
            raise KnowledgeError("stale_revision", "identity revision changed")
        if str(target_id) == str(source_id):
            raise KnowledgeError("invalid_input", "cannot merge a person into itself")
        self.require_person(target_id)
        self.require_person(source_id)
        if self.canonical_id(target_id) != str(target_id):
            raise KnowledgeError("identity_conflict", "target is itself a merge source")
        if self.canonical_id(source_id) != str(source_id):
            raise KnowledgeError("identity_conflict", "source already has an active redirect")
        operation_id = self._store.new_id()
        ts = self._store.now_ms()
        self._store.execute(
            "INSERT INTO knowledge_identity_redirects (operation_id, seq, source_id, target_id,"
            " actor_principal, authorization_ref, created_ms, active)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
            (
                operation_id,
                self._store.next_seq(),
                str(source_id),
                str(target_id),
                context.actor_principal,
                context.authorization_ref,
                ts,
            ),
        )
        self._store.execute(
            "INSERT INTO knowledge_identity_ops (operation_id, kind, actor_principal,"
            " authorization_ref, payload_json, created_ms, undone)"
            " VALUES (?, 'merge', ?, ?, ?, ?, 0)",
            (
                operation_id,
                context.actor_principal,
                context.authorization_ref,
                _json({"source_id": str(source_id), "target_id": str(target_id)}),
                ts,
            ),
        )
        revision = self._store.bump_identity_revision()
        return ChangeReceipt(
            operation_id=operation_id,
            identity_revision=revision,
            acl_epoch=self._store.acl_epoch,
            changed_ids=(str(target_id), str(source_id)),
        )

    def dependent_operations(self, operation_id: str) -> tuple[str, ...]:
        """Active redirects that build on this one and must be undone first.

        Undoing ``c -> b`` while ``b -> a`` is still active would move the canonical
        person of ``c`` from ``a`` back to ``c``, because the target of this redirect is
        itself redirected.  Redirects that merely *point at* this one are unaffected:
        their source keeps its canonical person either way.
        """
        redirects = [item for item in self.active_redirects() if item.active]
        start = next((item for item in redirects if item.operation_id == operation_id), None)
        if start is None:
            return ()
        return tuple(
            item.operation_id
            for item in redirects
            if item.operation_id != operation_id and item.source_id == start.target_id
        )

    def undo_merge(
        self, operation_id: str, *, expected_revision: int, context: TrustedAdminContext
    ) -> ChangeReceipt:
        """Remove exactly this redirect.  Refuses while dependents exist."""
        self._require_owner(context)
        self._policy.require_admin(context)
        if int(expected_revision) != self._store.identity_revision:
            raise KnowledgeError("stale_revision", "identity revision changed")
        row = self._store.query_one(
            "SELECT * FROM knowledge_identity_redirects WHERE operation_id = ?",
            (str(operation_id),),
        )
        if row is None:
            raise KnowledgeError("unresolved", "unknown merge operation")
        if not bool(row["active"]):
            raise KnowledgeError("identity_conflict", "merge operation is already undone")
        dependents = self.dependent_operations(str(operation_id))
        if dependents:
            raise KnowledgeError(
                "dependent_merge",
                "dependent merge operations must be undone first: " + ",".join(dependents),
            )
        ts = self._store.now_ms()
        self._store.execute(
            "UPDATE knowledge_identity_redirects SET active = 0, undone_ms = ?"
            " WHERE operation_id = ?",
            (ts, str(operation_id)),
        )
        self._store.execute(
            "UPDATE knowledge_identity_ops SET undone = 1 WHERE operation_id = ?",
            (str(operation_id),),
        )
        revision = self._store.bump_identity_revision()
        return ChangeReceipt(
            operation_id=str(operation_id),
            identity_revision=revision,
            acl_epoch=self._store.acl_epoch,
            changed_ids=(str(row["source_id"]), str(row["target_id"])),
        )

    # ── name observation helper used by capture ──────────────────────────────

    def person_by_display_or_alias(self, name: str) -> tuple[str, ...]:
        results = self.search_by_name(name, context=None, limit=50)
        return tuple(item.person_id for item in results if item.person_id)

    def eligible_people_for_source(
        self, candidate: StatementCandidate, context: TrustedCaptureContext
    ) -> tuple[str, ...]:
        """Person ids the extractor was allowed to reference for this capture.

        Model proposals may only use source-local references into this set; anything
        else is rejected instead of being trusted.
        """
        offered: list[str] = []
        speaker = candidate.sources[0].author_principal if candidate.sources else ""
        person_id = self.person_id_for_principal(speaker)
        if person_id:
            offered.append(self.canonical_id(person_id))
        for link in candidate.people:
            canonical = self.canonical_id(link.person_id)
            if canonical not in offered:
                offered.append(canonical)
        return tuple(offered)


def _iso(ms: int) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ms / 1000.0, tz=UTC).isoformat(timespec="seconds")


def _json(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, separators=(",", ":"), sort_keys=True)
