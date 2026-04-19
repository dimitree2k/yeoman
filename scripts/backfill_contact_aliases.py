"""One-shot: seed contact_aliases from the WhatsApp inbound archive.

Reads sender_name (WhatsApp pushName) from reply_context.db.inbound_messages
and inserts one row per (contact, alias) into contacts.db.contact_aliases with
source="push_name". Idempotent: re-running only bumps last_seen on existing
rows. Also promotes contacts.display_name from raw JID to the most-recent
push_name when the current display_name is still JID-shaped.

Run with:
    uv run python scripts/backfill_contact_aliases.py [--dry-run]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

ARCHIVE_DB = Path("~/.yeoman/data/inbound/reply_context.db").expanduser()
CONTACTS_DB = Path("~/.yeoman/data/contacts/contacts.db").expanduser()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _is_jid_shaped(name: str) -> bool:
    return "@s.whatsapp.net" in name or "@lid" in name or "@g.us" in name


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print without writing")
    args = parser.parse_args()

    if not ARCHIVE_DB.exists():
        print(f"archive db not found: {ARCHIVE_DB}", file=sys.stderr)
        return 1
    if not CONTACTS_DB.exists():
        print(f"contacts db not found: {CONTACTS_DB}", file=sys.stderr)
        return 1

    archive = sqlite3.connect(f"file:{ARCHIVE_DB}?mode=ro", uri=True)
    archive.row_factory = sqlite3.Row
    contacts = sqlite3.connect(str(CONTACTS_DB))
    contacts.row_factory = sqlite3.Row
    contacts.execute("PRAGMA foreign_keys=ON")

    # (channel, participant) -> {alias: latest_created_at}
    rows = archive.execute(
        """
        SELECT channel, participant, sender_name, MAX(created_at) AS latest
        FROM inbound_messages
        WHERE sender_name IS NOT NULL
          AND sender_name != ''
          AND participant IS NOT NULL
          AND participant != ''
        GROUP BY channel, participant, sender_name
        ORDER BY latest DESC
        """
    ).fetchall()

    identifiers = {
        (r["channel"], r["identifier"]): r["contact_id"]
        for r in contacts.execute(
            "SELECT channel, identifier, contact_id FROM contact_identifiers"
        ).fetchall()
    }

    aliases_added = 0
    aliases_touched = 0
    display_promoted = 0
    unmatched: list[tuple[str, str, str]] = []
    latest_per_contact: dict[str, tuple[str, str]] = {}

    now = _now_iso()
    for row in rows:
        channel = row["channel"]
        participant = row["participant"]
        alias = row["sender_name"].strip()
        latest = row["latest"]
        contact_id = identifiers.get((channel, participant))
        if contact_id is None:
            unmatched.append((channel, participant, alias))
            continue

        if contact_id not in latest_per_contact:
            latest_per_contact[contact_id] = (alias, latest)

        if args.dry_run:
            aliases_touched += 1
            continue

        existed = contacts.execute(
            "SELECT 1 FROM contact_aliases WHERE contact_id=? AND alias=? AND source=?",
            (contact_id, alias, "push_name"),
        ).fetchone()
        contacts.execute(
            """
            INSERT INTO contact_aliases (contact_id, alias, source, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (contact_id, alias, source)
            DO UPDATE SET last_seen = excluded.last_seen
            """,
            (contact_id, alias, "push_name", now, now),
        )
        if existed:
            aliases_touched += 1
        else:
            aliases_added += 1

    # Promote display_name from JID-shaped to most-recent push_name.
    for contact_id, (alias, _latest) in latest_per_contact.items():
        current = contacts.execute(
            "SELECT display_name FROM contacts WHERE id=?", (contact_id,)
        ).fetchone()
        if current is None:
            continue
        if not _is_jid_shaped(current["display_name"]):
            continue
        if args.dry_run:
            display_promoted += 1
            continue
        contacts.execute(
            "UPDATE contacts SET display_name=?, updated_at=? WHERE id=?",
            (alias, now, contact_id),
        )
        display_promoted += 1

    if not args.dry_run:
        contacts.commit()

    print(f"aliases added:    {aliases_added}")
    print(f"aliases refreshed:{aliases_touched}")
    print(f"display names promoted: {display_promoted}")
    print(f"unmatched archive rows (no contact record): {len(unmatched)}")
    if unmatched[:5]:
        for channel, participant, alias in unmatched[:5]:
            print(f"  - {channel} {participant} → {alias!r}")
        if len(unmatched) > 5:
            print(f"  ... +{len(unmatched) - 5} more")
    archive.close()
    contacts.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
