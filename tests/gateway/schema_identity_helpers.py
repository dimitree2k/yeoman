"""Read-only identity for synthetic v2 schema and upgrade fixtures.

Runtime tests use ProjectedHistory; these fixtures need the original frozen table reads
while constructing a v2 schema witness, before its upgrade to the live v3 contract.
"""
from __future__ import annotations

from yeoman_gateway.knowledge._person_identity import IdentityEngine, PersonRow
from yeoman_gateway.knowledge.models import KnowledgeError


class SyntheticSchemaIdentity(IdentityEngine):

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

