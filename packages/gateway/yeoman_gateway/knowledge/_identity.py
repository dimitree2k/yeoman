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

    def people_for_statement(self, statement_id: str) -> tuple[tuple[str, str, str, int], ...]:
        """Raw role edges of a statement: (person_id, role, evidence_event, revision)."""
        rows = self._store.query(
            "SELECT person_id, role, evidence_source_id, evidence_revision"
            " FROM knowledge_statement_people WHERE statement_id = ?"
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
            f" WHERE statement_id IN ({placeholders})"
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
            f" WHERE person_id IN ({placeholders}) AND role IN ({role_placeholders})",
            (*canonical, *roles),
        )
        allowed = {self.canonical_id(str(row["person_id"])) for row in rows}
        return tuple(item for item in canonical if item in allowed)

    # ── identifiers ──────────────────────────────────────────────────────────

    def binding_for(self, identifier: Identifier) -> IdentifierBinding | None:
        row = self._store.query_one(
            "SELECT * FROM knowledge_identifier_bindings"
            " WHERE channel = ? AND kind = ? AND value = ?",
            identifier.key,
        )
        if row is None:
            return None
        return self._row_to_binding(row)

    def bindings_of(self, person_id: str) -> tuple[IdentifierBinding, ...]:
        rows = self._store.query(
            "SELECT * FROM knowledge_identifier_bindings WHERE person_id = ?"
            " ORDER BY channel, kind, value",
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
                channel=str(row["channel"]), kind=str(row["kind"]), value=str(row["value"])
            ),
            evidence_ref=str(row["evidence_ref"]),
            status=str(row["status"]),
            mapping_verified=bool(row["mapping_verified"]),
            created_ms=int(row["created_ms"]),
            updated_ms=int(row["updated_ms"]),
        )

    def _bind(
        self,
        *,
        person_id: str,
        identifier: Identifier,
        evidence_ref: str,
        mapping_verified: bool,
        status: str = "active",
    ) -> None:
        ts = now_ms()
        self._store.execute(
            """
            INSERT INTO knowledge_identifier_bindings
                (channel, kind, value, person_id, status, evidence_ref,
                 mapping_verified, created_ms, updated_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel, kind, value) DO UPDATE SET
                status = excluded.status,
                updated_ms = excluded.updated_ms
            """,
            (
                identifier.channel,
                identifier.kind,
                identifier.value,
                person_id,
                status,
                evidence_ref,
                int(mapping_verified),
                ts,
                ts,
            ),
        )
        # Keep the legacy contact_identifiers cache in step for the migration window.
        self._store.execute(
            "INSERT INTO contact_identifiers (channel, identifier, contact_id, kind)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(channel, identifier) DO UPDATE SET"
            " contact_id = excluded.contact_id, kind = excluded.kind",
            (identifier.channel, identifier.value, person_id, identifier.kind),
        )

    def _create_stub(
        self,
        *,
        identifier: Identifier,
        evidence_ref: str,
        observed_name: str | None,
        mapping_verified: bool,
    ) -> str:
        person_id = self._store.new_id()
        ts = now_ms()
        iso = _iso(ts)
        display = observed_name or identifier.value
        self._store.execute(
            "INSERT INTO contacts (id, display_name, phone_number, is_owner, created_at,"
            " updated_at, revision, status, preferred_name_visibility)"
            " VALUES (?, ?, NULL, 0, ?, ?, 1, 'active', 'public')",
            (person_id, display, iso, iso),
        )
        self._bind(
            person_id=person_id,
            identifier=identifier,
            evidence_ref=evidence_ref,
            mapping_verified=mapping_verified,
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
    ) -> None:
        clean = validate_name(name)
        iso = _iso(ts)
        self._store.execute(
            """
            INSERT INTO contact_aliases
                (contact_id, alias, source, first_seen, last_seen, visibility,
                 first_seen_ms, last_seen_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(contact_id, alias, source) DO UPDATE SET
                last_seen = excluded.last_seen,
                last_seen_ms = excluded.last_seen_ms
            """,
            (person_id, clean, source, iso, iso, visibility, ts, ts),
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
        return tuple(
            NameObservation(
                person_id=str(row["contact_id"]),
                name=str(row["alias"]),
                source=str(row["source"]),
                visibility=str(row["visibility"] or "public"),
                first_seen_ms=int(row["first_seen_ms"] or 0),
                last_seen_ms=int(row["last_seen_ms"] or 0),
            )
            for row in rows
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
            out.setdefault(str(row["contact_id"]), []).append(
                NameObservation(
                    person_id=str(row["contact_id"]),
                    name=str(row["alias"]),
                    source=str(row["source"]),
                    visibility=str(row["visibility"] or "public"),
                    first_seen_ms=int(row["first_seen_ms"] or 0),
                    last_seen_ms=int(row["last_seen_ms"] or 0),
                )
            )
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

        existing: dict[tuple[str, str, str], IdentifierBinding] = {}
        for identifier in observation.identifiers:
            binding = self.binding_for(identifier)
            if binding is not None and binding.status == "active":
                existing[identifier.key] = binding

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
                    if identifier.key in existing:
                        continue
                    if identifier.kind in _REASSIGNABLE_KINDS:
                        # A recyclable handle is not durable identity evidence.
                        continue
                    self._bind(
                        person_id=person_id,
                        identifier=identifier,
                        evidence_ref=evidence_ref,
                        mapping_verified=True,
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
                    ts=int(observation.observed_at_ms or now_ms()),
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
                if identifier.key == primary.key:
                    continue
                if identifier.kind in _REASSIGNABLE_KINDS:
                    continue
                self._bind(
                    person_id=person_id,
                    identifier=identifier,
                    evidence_ref=evidence_ref,
                    mapping_verified=True,
                )
        return PersonResolution(
            status="resolved",
            person_id=person_id,
            display_name=self.display_name(person_id, context=context),
            identity_revision=self._store.identity_revision,
            reason="created_stub_from_verified_identifier",
        )

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
        """Map a security principal to a person *through proven identifier bindings*.

        Real principals are channel-qualified (``whatsapp:491...``); the mapping is a
        lookup, never a guess from a display name.
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
            row = self._store.query_one(
                "SELECT person_id FROM knowledge_identifier_bindings"
                " WHERE channel = ? AND value = ? AND status = 'active' LIMIT 1",
                (candidate_channel, candidate_value),
            )
            if row is not None:
                return str(row["person_id"])
            legacy = self._store.query_one(
                "SELECT contact_id FROM contact_identifiers"
                " WHERE channel = ? AND identifier = ? LIMIT 1",
                (candidate_channel, candidate_value),
            )
            if legacy is not None:
                return str(legacy["contact_id"])
        return None

    def resolve_endpoint(
        self,
        person_id: str,
        channel: str,
        *,
        prefer_kind: str | None = None,
    ) -> EndpointResolution:
        """One proven endpoint - or an explicit error, never a random match.

        Phone and LID have no built-in order: a caller that wants the phone JID (or the
        LID) of a person says so with ``prefer_kind``.  Without a preference, two kinds
        on the same channel stay ``ambiguous`` instead of silently picking one.
        """
        canonical = self.canonical_id(person_id)
        revision = self._store.identity_revision
        if self.get_person(canonical) is None:
            return EndpointResolution(
                status="unresolved",
                person_id=canonical,
                identifier=None,
                identity_revision=revision,
                reason="unknown_person",
            )
        channel_key = str(channel).strip().lower()
        rows = self._store.query(
            "SELECT * FROM knowledge_identifier_bindings"
            " WHERE person_id = ? AND channel = ? AND status = 'active'"
            " ORDER BY kind, value",
            (canonical, channel_key),
        )
        bindings = [self._row_to_binding(row) for row in rows]
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
        authorization = self._policy.require_admin(context)
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
