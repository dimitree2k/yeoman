"""One-shot: re-scope existing person_profile memory rows from user: to contact:.

The _resolve_contact_id fix lets future writes hit contact scope directly, but
historical rows (95 from the backfill + a few from live) sit under
channel:<ch>:user:<token>. This migration rewrites their scope_key to
contact:<uuid> for every sender that the contacts cache can now resolve.

Idempotent: re-running finds nothing left to migrate.

Usage:
    uv run python scripts/migrate_person_profile_scope.py [--dry-run]
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

from yeoman_gateway.contacts.service import ContactsService
from yeoman_shared.config.loader import load_config
from yeoman_shared.utils.helpers import get_operational_data_path


USER_SCOPE_RE = re.compile(r"^channel:[^:]+:user:(.+)$")


def _resolve(contacts_jids: dict[str, str], token: str) -> str | None:
    direct = contacts_jids.get(token)
    if direct:
        return direct
    if "@" in token:
        return None
    return contacts_jids.get(f"{token}@s.whatsapp.net") or contacts_jids.get(f"{token}@lid")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config = load_config()
    memory_db = Path(config.memory.db_path).expanduser().resolve()
    contacts = ContactsService(
        db_path=get_operational_data_path() / "contacts" / "contacts.db"
    )
    jids = contacts.known_jids
    if not jids:
        print("contacts cache is empty — nothing to migrate", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(memory_db))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")

    rows = conn.execute(
        """
        SELECT id, scope_key, content_hash
        FROM memory2_nodes
        WHERE is_deleted = 0
          AND kind = 'person_profile'
          AND scope_type = 'user'
        """
    ).fetchall()

    migrated = 0
    unresolved = 0
    duplicate_soft_deleted = 0
    now_iso = datetime.now(UTC).isoformat()

    for row in rows:
        m = USER_SCOPE_RE.match(row["scope_key"])
        if not m:
            continue
        token = m.group(1)
        contact_id = _resolve(jids, token)
        if not contact_id:
            unresolved += 1
            continue
        new_scope_key = f"contact:{contact_id}"
        if args.dry_run:
            migrated += 1
            continue
        try:
            conn.execute(
                """
                UPDATE memory2_nodes
                SET scope_type = 'contact',
                    scope_key = ?,
                    contact_id = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (new_scope_key, contact_id, now_iso, row["id"]),
            )
            migrated += 1
        except sqlite3.IntegrityError:
            # UNIQUE (workspace_id, scope_key, sector, content_hash, is_deleted)
            # collision: another row already occupies the target contact scope
            # with the same content_hash. Soft-delete this duplicate.
            conn.execute(
                "UPDATE memory2_nodes SET is_deleted = 1, updated_at = ? WHERE id = ?",
                (now_iso, row["id"]),
            )
            duplicate_soft_deleted += 1

    if not args.dry_run:
        conn.commit()
    conn.close()

    print(f"candidates scanned:          {len(rows)}")
    print(f"migrated to contact scope:   {migrated}")
    print(f"duplicates soft-deleted:     {duplicate_soft_deleted}")
    print(f"unresolved (no contact):     {unresolved}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
