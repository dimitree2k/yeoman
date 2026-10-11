"""Private identity engine: people, identifier bindings, names and merges.

Nothing here decides read permission, owner status or delivery targets.  Identity is a
naming and provenance problem; authorization stays with Policy and with
``knowledge._retrieval``.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Final, Iterable

from yeoman_gateway.knowledge._store import KnowledgeStore
from yeoman_gateway.knowledge.models import (
    DEFAULT_NAMESPACE,
    GLOBAL_SCOPE_KEY,
    EndpointResolution,
    Identifier,
    IdentifierBinding,
    KnowledgeError,
    NameObservation,
    PersonResolution,
    StatementCandidate,
    TrustedCaptureContext,
    TrustedReadContext,
    ValidationError,
    normalize_alias_value,
    validate_name,
)

NAME_SOURCE_PRIORITY: Final[tuple[str, ...]] = ("owner_confirmed", "self_reported", "observed")

#: Identifier kinds whose values may be re-assigned by a platform (handles, usernames,
#: telegram ids of recycled accounts).  They never create a durable trusted identity.
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
    """Shared read helpers used by history-scoped identity lookup."""

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
                if item.source == source
                and item.findable
                and (item.visibility == "public" or allow_private)
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


    def provider_merge_protection_reason(
        self,
        person_ids: Iterable[str],
        *,
        additional_identifiers: Iterable[Identifier] = (),
    ) -> str | None:
        """Reject new provider stitching that would leave multiple phone/LID values.

        Existing owner-directed merges remain governed by their separate admin API. This
        guard is shared by provider stitching and pending duplicate proposals.
        """
        canonical_people = {self.canonical_id(item) for item in person_ids}
        values: dict[tuple[str, str, str], set[str]] = {}
        if canonical_people:
            for row in self._store.query(
                "SELECT person_id, channel, kind, namespace, value"
                " FROM knowledge_identifier_bindings"
                " WHERE status = 'active' AND kind IN ('phone_jid','lid')"
            ):
                if self.canonical_id(str(row["person_id"])) not in canonical_people:
                    continue
                key = (str(row["channel"]), str(row["namespace"]), str(row["kind"]))
                values.setdefault(key, set()).add(str(row["value"]))
        for identifier in additional_identifiers:
            if identifier.kind not in {"phone_jid", "lid"}:
                continue
            key = (identifier.channel, str(identifier.namespace), identifier.kind)
            values.setdefault(key, set()).add(identifier.value)
        for (_channel, _namespace, _kind), identifiers in values.items():
            if len(identifiers) > 1:
                return "multiple_phone_or_lid_values_in_account_namespace"
        return None








    # ── names and aliases ────────────────────────────────────────────────────

    def alias_by_id(self, alias_id: int) -> NameObservation | None:
        row = self._store.query_one(
            "SELECT * FROM contact_aliases WHERE id = ?", (int(alias_id),)
        )
        return None if row is None else self._row_to_alias(row)





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
            # A merge keeps the identifier bindings on their original person row, so the
            # bindings of every member have to be gathered, not only the canonical one.
            for member in self.merged_member_ids(person_id):
                for binding in self.active_bindings_of(member):
                    identifier = binding.identifier
                    if identifier.channel != channel_key or identifier.full_key in seen:
                        continue
                    seen.add(identifier.full_key)
                    identifiers.append(identifier)
        return tuple(sorted(identifiers, key=lambda item: (item.kind, item.value)))

    def owners_of_identifier_value(
        self, value: str, *, channel: str | None = None
    ) -> tuple[str, ...]:
        """Canonical people with an *active proven* binding for this identifier value.

        The namespace-blind twin of :meth:`resolve_identifier`, for a caller that only
        holds a value - a mention in a message, a dialled number.  It returns every owner
        instead of a first match, and an unproven ``contact_identifiers`` projection is
        never an owner.
        """
        token = str(value or "").strip()
        if not token:
            return ()
        candidates = [token]
        local = token.split("@", 1)[0]
        if local and local != token:
            candidates.append(local)
        channel_key = str(channel or "").strip().lower()
        owners: list[str] = []
        for candidate in dict.fromkeys(candidates):
            sql = (
                "SELECT person_id FROM knowledge_identifier_bindings"
                " WHERE value = ? AND status = 'active'"
            )
            params: tuple[object, ...] = (candidate,)
            if channel_key:
                sql += " AND channel = ?"
                params = (candidate, channel_key)
            for row in self._store.query(sql, params):
                canonical = self.canonical_id(str(row["person_id"]))
                if canonical not in owners:
                    owners.append(canonical)
        return tuple(owners)

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

    def search_mention_name_candidates(
        self,
        name_token: str,
        *,
        person_ids: tuple[str, ...],
        context: TrustedReadContext,
    ) -> tuple[PersonResolution, ...]:
        """Find name candidates only among offered people and visible scoped aliases."""
        token = normalize_alias_value(validate_name(name_token, "mention name token"))
        if not token:
            return ()
        allowed_scopes = {GLOBAL_SCOPE_KEY, context.scope_key()}
        results: list[PersonResolution] = []
        for person_id in sorted({self.canonical_id(item) for item in person_ids}):
            person = self.get_person(person_id)
            if person is None or person.status != "active":
                continue
            names = [
                alias
                for member_id in self.merged_member_ids(person_id)
                for alias in self.aliases_of(member_id)
                if alias.status in ("observed", "confirmed")
                and alias.findable
                and alias.scope_key in allowed_scopes
                and (alias.visibility == "public" or context.is_direct)
                and (alias.valid_until_ms is None or alias.valid_until_ms > context.now_ms)
            ]
            matching = [
                alias.name
                for alias in names
                if token in normalize_alias_value(alias.name)
            ]
            if not matching:
                continue
            matched_name = min(
                matching,
                key=lambda name: (
                    normalize_alias_value(name) != token,
                    len(normalize_alias_value(name)),
                    normalize_alias_value(name),
                ),
            )
            results.append(
                PersonResolution(
                    status="ambiguous",
                    person_id=person_id,
                    display_name=matched_name,
                    identity_revision=self._store.identity_revision,
                    reason="name_candidate_requires_confirmation",
                )
            )
        return tuple(results)

    def search_mention_text_candidates(
        self,
        content: str,
        *,
        person_ids: tuple[str, ...],
        context: TrustedReadContext,
    ) -> tuple[PersonResolution, ...]:
        """Match complete visible aliases in ordinary text, without resolving identity."""
        text = " ".join(content.casefold().split())
        if not text:
            return ()
        allowed_scopes = {GLOBAL_SCOPE_KEY, context.scope_key()}
        results: list[PersonResolution] = []
        for person_id in sorted({self.canonical_id(item) for item in person_ids}):
            person = self.get_person(person_id)
            if person is None or person.status != "active":
                continue
            eligible_names = {
                normalize_alias_value(alias.name)
                for member_id in self.merged_member_ids(person_id)
                for alias in self.aliases_of(member_id)
                if alias.status in ("observed", "confirmed")
                and alias.findable
                and alias.scope_key in allowed_scopes
                and (alias.visibility == "public" or context.is_direct)
                and (alias.valid_until_ms is None or alias.valid_until_ms > context.now_ms)
            }
            matches = sorted(
                name
                for name in eligible_names
                if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text)
            )
            if not matches:
                continue
            results.append(
                PersonResolution(
                    status="ambiguous",
                    person_id=person_id,
                    display_name=matches[0],
                    identity_revision=self._store.identity_revision,
                    reason="name_candidate_requires_confirmation",
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








    # ── merge / undo ─────────────────────────────────────────────────────────





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

    return json.dumps(payload, separators=(",", ":"), sort_keys=True)
