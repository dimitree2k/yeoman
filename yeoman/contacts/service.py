"""Business-logic layer for contacts CRM with in-memory caching."""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from yeoman.contacts.store import ContactsStore


class ContactsService:
    """Wraps ContactsStore with in-memory JID cache and business logic.

    Attributes:
        store: The underlying SQLite store.
        known_jids: Mapping of identifier -> contact_id, loaded on boot.
    """

    def __init__(self, db_path: Path) -> None:
        self.store = ContactsStore(db_path=db_path)
        self.known_jids: dict[str, str] = {}
        self._display_names: dict[str, str] = {}
        self.reload_cache()

    # ── cache management ──────────────────────────────────────────────────

    def reload_cache(self) -> None:
        """(Re)load the in-memory JID cache from the store."""
        self.known_jids = self.store.load_all_identifiers()
        self._display_names.clear()
        logger.debug("contacts cache reloaded — {} known JIDs", len(self.known_jids))

    # ── ensure_contact ────────────────────────────────────────────────────

    def ensure_contact(
        self,
        *,
        channel: str,
        identifier: str,
        kind: str,
        push_name: str | None = None,
    ) -> str:
        """Return the contact_id for an identifier, creating a stub if needed.

        If *push_name* is provided it is tracked as an alias (source="push_name").
        """
        # Fast path: already in cache
        if identifier in self.known_jids:
            contact_id = self.known_jids[identifier]
            if push_name:
                self._track_alias(contact_id, push_name)
            return contact_id

        # Check the DB (another process may have inserted)
        existing = self.store.lookup_by_identifier(channel, identifier)
        if existing is not None:
            self.known_jids[identifier] = existing.id
            if push_name:
                self._track_alias(existing.id, push_name)
            return existing.id

        # Create a new stub contact
        display = push_name or identifier
        contact = self.store.create_contact(display_name=display)
        self.store.add_identifier(
            contact_id=contact.id,
            channel=channel,
            identifier=identifier,
            kind=kind,
        )
        self.known_jids[identifier] = contact.id

        if push_name:
            self._track_alias(contact.id, push_name)

        logger.info(
            "new contact stub: {} ({}) → {}",
            display, identifier, contact.id,
        )
        return contact.id

    def _track_alias(self, contact_id: str, push_name: str) -> None:
        """Upsert a push_name alias for the given contact."""
        self.store.upsert_alias(
            contact_id=contact_id,
            alias=push_name,
            source="push_name",
        )

    # ── display name ──────────────────────────────────────────────────────

    def update_display_name(self, contact_id: str, display_name: str) -> None:
        """Update display name in the store and invalidate the cache entry."""
        self.store.update_display_name(contact_id, display_name)
        self._display_names.pop(contact_id, None)

    def get_display_name(self, contact_id: str) -> str | None:
        """Return the display name for a contact, using cache when possible."""
        if contact_id in self._display_names:
            return self._display_names[contact_id]

        contact = self.store.get_contact(contact_id)
        if contact is None:
            return None

        self._display_names[contact_id] = contact.display_name
        return contact.display_name

    # ── name resolution ───────────────────────────────────────────────────

    def resolve_name_to_jid(
        self,
        name: str,
        *,
        channel: str,
        group_participants: list[str] | None = None,
    ) -> str | None:
        """Resolve a human name to a JID on *channel*.

        Search order: display_name, then aliases.  If multiple contacts match
        and *group_participants* is provided, narrow to participants in that
        list.  Return ``None`` if still ambiguous or no match.
        """
        # Collect candidate (contact_id, identifier) pairs
        candidates: list[tuple[str, str]] = []

        # Search by display name
        contacts_by_name = self.store.search_by_display_name(name)
        for contact in contacts_by_name:
            if contact.display_name.lower() != name.lower():
                continue  # search_by_display_name uses LIKE %...%, we want exact
            for ident in self.store.get_identifiers(contact.id):
                if ident.channel == channel:
                    candidates.append((contact.id, ident.identifier))

        # Search by alias if no display-name matches
        if not candidates:
            contacts_by_alias = self.store.search_by_alias(name)
            for contact in contacts_by_alias:
                # Verify exact alias match (case-insensitive)
                aliases = self.store.get_aliases(contact.id)
                if not any(a.alias.lower() == name.lower() for a in aliases):
                    continue
                for ident in self.store.get_identifiers(contact.id):
                    if ident.channel == channel:
                        candidates.append((contact.id, ident.identifier))

        if not candidates:
            return None

        if len(candidates) == 1:
            return candidates[0][1]

        # Ambiguous — try to narrow with group_participants
        if group_participants:
            narrowed = [
                (cid, ident) for cid, ident in candidates
                if ident in group_participants
            ]
            if len(narrowed) == 1:
                return narrowed[0][1]

        # Still ambiguous or no group hint
        return None

    # ── owner marking ─────────────────────────────────────────────────────

    def mark_owner_from_policy(self, owner_map: dict[str, list[str]]) -> None:
        """Mark contacts whose identifiers appear in the policy owner lists.

        *owner_map* is ``{"whatsapp": ["jid1", ...], "telegram": ["tid1", ...]}``.
        """
        for channel, jids in owner_map.items():
            for jid in jids:
                contact = self.store.lookup_by_identifier(channel, jid)
                if contact is not None:
                    self.store.set_owner(contact.id, is_owner=True)
                    logger.info("marked {} as owner ({})", contact.display_name, jid)

    # ── roster for disclosure ────────────────────────────────────────

    def build_roster(
        self,
        *,
        channel: str,
        participant_jids: list[str],
    ) -> list[dict[str, object]]:
        """Build roster entries for known, non-owner participants."""
        roster: list[dict[str, object]] = []
        for jid in participant_jids:
            contact_id = self.known_jids.get(jid)
            if not contact_id:
                continue
            contact = self.store.get_contact(contact_id)
            if contact is None or contact.is_owner:
                continue
            facts: list[str] = []
            for f in self.store.get_fields(contact_id):
                label = f" ({f.label})" if f.label else ""
                if f.kind == "note":
                    facts.append(f.value)
                else:
                    facts.append(f"{f.kind}{label}: {f.value}")
            roster.append({"name": contact.display_name, "facts": facts})
        return roster

    def format_roster_text(
        self,
        *,
        channel: str,
        participant_jids: list[str],
    ) -> str:
        """Format roster as text block for LLM context injection."""
        roster = self.build_roster(channel=channel, participant_jids=participant_jids)
        if not roster:
            return ""
        lines = ["[Group Members]"]
        for entry in roster:
            facts_str = ", ".join(str(f) for f in entry["facts"]) if entry["facts"] else ""
            if facts_str:
                lines.append(f"- {entry['name']}: {facts_str}")
            else:
                lines.append(f"- {entry['name']}")
        return "\n".join(lines)

    # ── memory backfill ────────────────────────────────────────────────────

    def backfill_memory(self, memory_store: object) -> int:
        """One-time backfill: link existing memory nodes to contacts by sender_id."""
        linked = 0
        for identifier, contact_id in self.known_jids.items():
            count = memory_store.link_nodes_to_contact(identifier, contact_id)  # type: ignore[attr-defined]
            linked += count
        return linked

    # ── lifecycle ─────────────────────────────────────────────────────────

    def close(self) -> None:
        """Close the underlying store."""
        self.store.close()
