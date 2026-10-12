"""Shared person read operations; history owns identity and redirects."""


from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from yeoman_gateway.knowledge._store import KnowledgeStore
from yeoman_gateway.knowledge.models import (
    GLOBAL_SCOPE_KEY,
    Identifier,
    IdentifierBinding,
    KnowledgeError,
    NameObservation,
    PersonResolution,
    TrustedReadContext,
    normalize_alias_value,
    validate_name,
)


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
    """Read operations shared by the projected history identity adapter."""


    def __init__(self, store: KnowledgeStore, *, authority: Any, policy: Any) -> None:
        self._store = store
        self._authority = authority
        self._policy = policy


    def require_person(self, person_id: str) -> PersonRow:
        person = self.get_person(person_id)
        if person is None:
            raise KnowledgeError("unresolved", f"unknown person: {person_id}")
        return person


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


    def active_bindings_of(self, person_id: str) -> tuple[IdentifierBinding, ...]:
        return tuple(item for item in self.bindings_of(person_id) if item.status == "active")


    def neutral_address(self, identifier: Identifier | None) -> str | None:
        if identifier is None:
            return None
        return identifier.value


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


    def person_by_display_or_alias(self, name: str) -> tuple[str, ...]:
        results = self.search_by_name(name, context=None, limit=50)
        return tuple(item.person_id for item in results if item.person_id)


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

