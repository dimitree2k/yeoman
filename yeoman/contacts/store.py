"""SQLite storage backend for contacts CRM."""

from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from yeoman.contacts.models import Contact, ContactAlias, ContactField, ContactIdentifier
from yeoman.utils.helpers import ensure_dir


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class ContactsStore:
    """Thread-safe SQLite CRUD for contacts, identifiers, aliases, and fields."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path.expanduser()
        ensure_dir(self.db_path.parent)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._create_schema()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── schema ───────────────────────────────────────────────────────────

    def _create_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS contacts (
                    id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    phone_number TEXT,
                    is_owner INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS contact_identifiers (
                    channel TEXT NOT NULL,
                    identifier TEXT NOT NULL,
                    contact_id TEXT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    PRIMARY KEY (channel, identifier)
                );

                CREATE INDEX IF NOT EXISTS idx_ci_contact
                    ON contact_identifiers (contact_id);

                CREATE TABLE IF NOT EXISTS contact_aliases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    contact_id TEXT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
                    alias TEXT NOT NULL,
                    source TEXT NOT NULL,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    UNIQUE (contact_id, alias, source)
                );

                CREATE INDEX IF NOT EXISTS idx_ca_contact
                    ON contact_aliases (contact_id);

                CREATE INDEX IF NOT EXISTS idx_ca_alias
                    ON contact_aliases (alias COLLATE NOCASE);

                CREATE TABLE IF NOT EXISTS contact_fields (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    contact_id TEXT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    value TEXT NOT NULL,
                    label TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_cf_contact
                    ON contact_fields (contact_id);
                """
            )
            self._conn.commit()

    # ── row converters ───────────────────────────────────────────────────

    @staticmethod
    def _row_to_contact(row: sqlite3.Row) -> Contact:
        return Contact(
            id=str(row["id"]),
            display_name=str(row["display_name"]),
            phone_number=str(row["phone_number"]) if row["phone_number"] else None,
            is_owner=bool(int(row["is_owner"])),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _row_to_identifier(row: sqlite3.Row) -> ContactIdentifier:
        return ContactIdentifier(
            contact_id=str(row["contact_id"]),
            channel=str(row["channel"]),
            identifier=str(row["identifier"]),
            kind=str(row["kind"]),
        )

    @staticmethod
    def _row_to_alias(row: sqlite3.Row) -> ContactAlias:
        return ContactAlias(
            contact_id=str(row["contact_id"]),
            alias=str(row["alias"]),
            source=str(row["source"]),
            first_seen=str(row["first_seen"]),
            last_seen=str(row["last_seen"]),
        )

    @staticmethod
    def _row_to_field(row: sqlite3.Row) -> ContactField:
        return ContactField(
            contact_id=str(row["contact_id"]),
            kind=str(row["kind"]),
            value=str(row["value"]),
            label=str(row["label"]) if row["label"] else None,
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    # ── contacts CRUD ────────────────────────────────────────────────────

    def create_contact(
        self,
        display_name: str,
        *,
        phone_number: str | None = None,
        is_owner: bool = False,
    ) -> Contact:
        contact_id = str(uuid.uuid4())
        now = _now_iso()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO contacts (id, display_name, phone_number, is_owner, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (contact_id, display_name, phone_number, int(is_owner), now, now),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM contacts WHERE id = ?", (contact_id,)
            ).fetchone()
        return self._row_to_contact(row)

    def get_contact(self, contact_id: str) -> Contact | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM contacts WHERE id = ?", (contact_id,)
            ).fetchone()
        if row is None:
            return None
        return self._row_to_contact(row)

    def update_display_name(self, contact_id: str, display_name: str) -> None:
        now = _now_iso()
        with self._lock:
            self._conn.execute(
                "UPDATE contacts SET display_name = ?, updated_at = ? WHERE id = ?",
                (display_name, now, contact_id),
            )
            self._conn.commit()

    def set_owner(self, contact_id: str, is_owner: bool = True) -> None:
        now = _now_iso()
        with self._lock:
            self._conn.execute(
                "UPDATE contacts SET is_owner = ?, updated_at = ? WHERE id = ?",
                (int(is_owner), now, contact_id),
            )
            self._conn.commit()

    def search_by_display_name(self, query: str) -> list[Contact]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM contacts WHERE display_name LIKE ? COLLATE NOCASE",
                (f"%{query}%",),
            ).fetchall()
        return [self._row_to_contact(r) for r in rows]

    # ── identifiers ──────────────────────────────────────────────────────

    def add_identifier(
        self,
        *,
        contact_id: str,
        channel: str,
        identifier: str,
        kind: str,
    ) -> ContactIdentifier:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO contact_identifiers (channel, identifier, contact_id, kind)
                VALUES (?, ?, ?, ?)
                """,
                (channel, identifier, contact_id, kind),
            )
            self._conn.commit()
        return ContactIdentifier(
            contact_id=contact_id,
            channel=channel,
            identifier=identifier,
            kind=kind,
        )

    def lookup_by_identifier(self, channel: str, identifier: str) -> Contact | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT c.*
                FROM contacts c
                JOIN contact_identifiers ci ON ci.contact_id = c.id
                WHERE ci.channel = ? AND ci.identifier = ?
                LIMIT 1
                """,
                (channel, identifier),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_contact(row)

    def get_identifiers(self, contact_id: str) -> list[ContactIdentifier]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM contact_identifiers WHERE contact_id = ?",
                (contact_id,),
            ).fetchall()
        return [self._row_to_identifier(r) for r in rows]

    def load_all_identifiers(self) -> dict[str, str]:
        """Return mapping of identifier -> contact_id for all rows."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT identifier, contact_id FROM contact_identifiers"
            ).fetchall()
        return {str(r["identifier"]): str(r["contact_id"]) for r in rows}

    # ── aliases ──────────────────────────────────────────────────────────

    def upsert_alias(
        self,
        *,
        contact_id: str,
        alias: str,
        source: str,
    ) -> None:
        now = _now_iso()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO contact_aliases (contact_id, alias, source, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (contact_id, alias, source)
                DO UPDATE SET last_seen = excluded.last_seen
                """,
                (contact_id, alias, source, now, now),
            )
            self._conn.commit()

    def get_aliases(self, contact_id: str) -> list[ContactAlias]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM contact_aliases WHERE contact_id = ?",
                (contact_id,),
            ).fetchall()
        return [self._row_to_alias(r) for r in rows]

    def search_by_alias(self, query: str) -> list[Contact]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT DISTINCT c.*
                FROM contacts c
                JOIN contact_aliases ca ON ca.contact_id = c.id
                WHERE ca.alias LIKE ? COLLATE NOCASE
                """,
                (f"%{query}%",),
            ).fetchall()
        return [self._row_to_contact(r) for r in rows]

    # ── fields ───────────────────────────────────────────────────────────

    def add_field(
        self,
        *,
        contact_id: str,
        kind: str,
        value: str,
        label: str | None = None,
    ) -> ContactField:
        now = _now_iso()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO contact_fields (contact_id, kind, value, label, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (contact_id, kind, value, label, now, now),
            )
            self._conn.commit()
        return ContactField(
            contact_id=contact_id,
            kind=kind,
            value=value,
            label=label,
            created_at=now,
            updated_at=now,
        )

    def get_fields(self, contact_id: str) -> list[ContactField]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM contact_fields WHERE contact_id = ?",
                (contact_id,),
            ).fetchall()
        return [self._row_to_field(r) for r in rows]

    def delete_field(self, contact_id: str, kind: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM contact_fields WHERE contact_id = ? AND kind = ? AND value = ?",
                (contact_id, kind, value),
            )
            self._conn.commit()

    # ── merge ────────────────────────────────────────────────────────────

    def merge_contacts(self, *, target_id: str, source_id: str) -> None:
        """Move all identifiers, aliases, and fields from source to target, then delete source."""
        with self._lock:
            # Move identifiers
            self._conn.execute(
                "UPDATE contact_identifiers SET contact_id = ? WHERE contact_id = ?",
                (target_id, source_id),
            )

            # Move aliases — delete duplicates that would violate UNIQUE(contact_id, alias, source)
            # first, then move the rest
            self._conn.execute(
                """
                DELETE FROM contact_aliases
                WHERE contact_id = ?
                  AND (alias, source) IN (
                      SELECT alias, source FROM contact_aliases WHERE contact_id = ?
                  )
                """,
                (source_id, target_id),
            )
            self._conn.execute(
                "UPDATE contact_aliases SET contact_id = ? WHERE contact_id = ?",
                (target_id, source_id),
            )

            # Move fields
            self._conn.execute(
                "UPDATE contact_fields SET contact_id = ? WHERE contact_id = ?",
                (target_id, source_id),
            )

            # Delete source contact
            self._conn.execute(
                "DELETE FROM contacts WHERE id = ?",
                (source_id,),
            )
            self._conn.commit()
