"""Chat registry for tracking metadata across channels."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.utils.helpers import ensure_dir, get_data_path, get_operational_data_path

PURGE_INTERVAL_SECONDS = 3600


class ChatType(Enum):
    """Chat type classification."""

    GROUP = "group"
    DM = "dm"
    BROADCAST = "broadcast"
    CHANNEL = "channel"
    UNKNOWN = "unknown"


class ChatRegistry:
    """SQLite-backed registry for chat metadata across channels."""

    def __init__(
        self,
        db_path: Path | None = None,
    ) -> None:
        if db_path is not None:
            self.db_path = db_path
        else:
            self.db_path = get_operational_data_path() / "inbound" / "chat_registry.db"
            self._migrate_legacy_default_path()
        ensure_dir(self.db_path.parent)

        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._create_schema()
        self._last_purge_at = 0.0

    def _migrate_legacy_default_path(self) -> None:
        """Move legacy ~/.nanobot/inbound/chat_registry.db into ~/.nanobot/data/inbound/."""
        legacy = get_data_path() / "inbound" / "chat_registry.db"
        if legacy == self.db_path:
            return
        if not legacy.exists() or self.db_path.exists():
            return
        ensure_dir(self.db_path.parent)
        try:
            for suffix in ("", "-wal", "-shm"):
                src = Path(f"{legacy}{suffix}")
                dst = Path(f"{self.db_path}{suffix}")
                if src.exists() and not dst.exists():
                    src.replace(dst)
        except OSError as e:
            logger.warning(f"Failed to migrate legacy chat registry DB from {legacy}: {e}")

    def _create_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chats (
                    channel TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    chat_type TEXT NOT NULL DEFAULT 'unknown',
                    readable_name TEXT,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT,
                    created_at INTEGER,
                    description TEXT,
                    owner_id TEXT,
                    participant_count INTEGER,
                    is_community BOOLEAN DEFAULT 0,
                    invite_code TEXT,
                    metadata_json TEXT,
                    last_sync_at TEXT,
                    PRIMARY KEY (channel, chat_id)
                )
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_chats_channel_type
                ON chats (channel, chat_type)
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_chats_first_seen
                ON chats (first_seen_at)
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_chats_last_seen
                ON chats (last_seen_at)
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_chats_readable_name
                ON chats (readable_name COLLATE NOCASE)
                """
            )
            self._conn.commit()

    def register_chat(
        self,
        *,
        channel: str,
        chat_id: str,
        chat_type: str | None = None,
        readable_name: str | None = None,
        created_at: int | None = None,
        description: str | None = None,
        owner_id: str | None = None,
        participant_count: int | None = None,
        is_community: bool | None = None,
        invite_code: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Register or update a chat. Returns True if newly registered, False if updated."""
        if not channel or not chat_id:
            logger.warning("register_chat: missing required fields")
            return False

        now_iso = datetime.now(UTC).isoformat()
        chat_type_safe = chat_type or ChatType.UNKNOWN.value
        community_flag = (
            1 if is_community is True else 0 if is_community is False else None
        )

        with self._lock:
            # Check if already exists
            existing = self._conn.execute(
                """
                SELECT first_seen_at FROM chats
                WHERE channel = ? AND chat_id = ?
                LIMIT 1
                """,
                (str(channel), str(chat_id)),
            ).fetchone()

            if existing:
                # Update existing chat
                self._conn.execute(
                    """
                    UPDATE chats SET
                        chat_type = COALESCE(?, chat_type),
                        readable_name = COALESCE(?, readable_name),
                        last_seen_at = ?,
                        created_at = COALESCE(?, created_at),
                        description = COALESCE(?, description),
                        owner_id = COALESCE(?, owner_id),
                        participant_count = COALESCE(?, participant_count),
                        is_community = COALESCE(?, is_community),
                        invite_code = COALESCE(?, invite_code),
                        metadata_json = COALESCE(?, metadata_json),
                        last_sync_at = ?
                    WHERE channel = ? AND chat_id = ?
                    """,
                    (
                        chat_type_safe,
                        readable_name,
                        now_iso,
                        int(created_at) if created_at else None,
                        description,
                        owner_id,
                        participant_count,
                        community_flag,
                        invite_code,
                        json.dumps(metadata) if metadata else None,
                        now_iso,
                        str(channel),
                        str(chat_id),
                    ),
                )
                self._conn.commit()
                return False
            else:
                # Insert new chat
                self._conn.execute(
                    """
                    INSERT INTO chats (
                        channel, chat_id, chat_type, readable_name,
                        first_seen_at, last_seen_at, created_at,
                        description, owner_id, participant_count,
                        is_community, invite_code, metadata_json, last_sync_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(channel),
                        str(chat_id),
                        chat_type_safe,
                        readable_name,
                        now_iso,
                        now_iso,
                        int(created_at) if created_at else None,
                        description,
                        owner_id,
                        participant_count,
                        community_flag,
                        invite_code,
                        json.dumps(metadata) if metadata else None,
                        now_iso,
                    ),
                )
                self._conn.commit()
                return True

    def get_chat(self, channel: str, chat_id: str) -> dict[str, Any] | None:
        """Get a single chat by channel and chat_id."""
        if not channel or not chat_id:
            return None

        with self._lock:
            row = self._conn.execute(
                """
                SELECT
                    channel, chat_id, chat_type, readable_name,
                    first_seen_at, last_seen_at, created_at,
                    description, owner_id, participant_count,
                    is_community, invite_code, metadata_json, last_sync_at
                FROM chats
                WHERE channel = ? AND chat_id = ?
                LIMIT 1
                """,
                (str(channel), str(chat_id)),
            ).fetchone()

        if row is None:
            return None

        result = dict(row)
        if result.get("metadata_json"):
            try:
                result["metadata"] = json.loads(result["metadata_json"])
            except (json.JSONDecodeError, TypeError):
                result["metadata"] = None
        del result["metadata_json"]
        return result

    def list_chats(
        self,
        *,
        channel: str | None = None,
        chat_type: str | None = None,
        limit: int | None = None,
        order_by: str = "first_seen_at",
        order_desc: bool = False,
    ) -> list[dict[str, Any]]:
        """List chats with optional filters."""
        conditions: list[str] = []
        params: list[Any] = []

        if channel:
            conditions.append("channel = ?")
            params.append(str(channel))

        if chat_type:
            conditions.append("chat_type = ?")
            params.append(str(chat_type))

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        order_dir = "DESC" if order_desc else "ASC"
        limit_clause = f"LIMIT {max(1, int(limit))}" if limit else ""

        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT
                    channel, chat_id, chat_type, readable_name,
                    first_seen_at, last_seen_at, created_at,
                    description, owner_id, participant_count,
                    is_community, invite_code, metadata_json, last_sync_at
                FROM chats
                {where_clause}
                ORDER BY {order_by} {order_dir}
                {limit_clause}
                """,
                params,
            ).fetchall()

        results = []
        for row in rows:
            result = dict(row)
            if result.get("metadata_json"):
                try:
                    result["metadata"] = json.loads(result["metadata_json"])
                except (json.JSONDecodeError, TypeError):
                    result["metadata"] = None
            del result["metadata_json"]
            results.append(result)

        return results

    def mark_seen(self, channel: str, chat_id: str) -> None:
        """Update last_seen_at timestamp."""
        if not channel or not chat_id:
            return

        now_iso = datetime.now(UTC).isoformat()
        with self._lock:
            self._conn.execute(
                """
                UPDATE chats SET last_seen_at = ?
                WHERE channel = ? AND chat_id = ?
                """,
                (now_iso, str(channel), str(chat_id)),
            )
            self._conn.commit()

    def search_chats(
        self,
        query: str,
        *,
        channel: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Search chats by readable name (case-insensitive)."""
        if not query:
            return []

        params = [f"%{query}%"]  # Prefix search
        if channel:
            params.append(str(channel))

        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT
                    channel, chat_id, chat_type, readable_name,
                    first_seen_at, last_seen_at
                FROM chats
                WHERE readable_name LIKE ?{" AND channel = ?" if channel else ""}
                ORDER BY last_seen_at DESC
                LIMIT {max(1, int(limit))}
                """,
                params,
            ).fetchall()

        return [dict(row) for row in rows]

    def get_channel_stats(self, channel: str) -> dict[str, Any]:
        """Get statistics for a channel."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT
                    COUNT(*) as total_chats,
                    COUNT(DISTINCT chat_type) as unique_types,
                    MIN(first_seen_at) as earliest_first_seen,
                    MAX(last_seen_at) as latest_last_seen
                FROM chats
                WHERE channel = ?
                """,
                (str(channel),),
            ).fetchone()

        if not row:
            return {"total_chats": 0, "unique_types": 0}

        result = dict(row)
        # Count by type
        by_type = self._conn.execute(
            """
            SELECT chat_type, COUNT(*) as count
            FROM chats
            WHERE channel = ?
            GROUP BY chat_type
            """,
            (str(channel),),
        ).fetchall()
        result["by_type"] = {r["chat_type"]: r["count"] for r in by_type}
        return result

    def migrate_from_seen_chats(self, seen_chats_path: Path) -> int:
        """Migrate legacy seen_chats.json format to registry."""
        if not seen_chats_path.exists():
            return 0

        try:
            data = json.loads(seen_chats_path.read_text())
            chats_list = data.get("chats", [])
            if not isinstance(chats_list, list):
                return 0

            migrated = 0
            for full_key in chats_list:
                if not isinstance(full_key, str):
                    continue

                # Parse "channel:chat_id" format
                if ":" not in full_key:
                    continue

                parts = full_key.split(":", 1)
                if len(parts) != 2:
                    continue

                channel, chat_id = parts
                if not channel or not chat_id:
                    continue

                # Infer chat type
                inferred_type = ChatType.UNKNOWN.value
                if chat_id.endswith("@g.us"):
                    inferred_type = ChatType.GROUP.value
                elif channel == "telegram":
                    if chat_id.startswith("-100"):
                        inferred_type = ChatType.GROUP.value
                    else:
                        inferred_type = ChatType.DM.value
                elif channel == "whatsapp":
                    if chat_id.endswith("@lid"):
                        inferred_type = ChatType.DM.value
                    elif chat_id.endswith("@s.whatsapp.net"):
                        inferred_type = ChatType.DM.value
                    else:
                        inferred_type = ChatType.GROUP.value

                # Try to infer chat name from policy or other sources
                # For now, just register with available info
                if self.register_chat(
                    channel=channel,
                    chat_id=chat_id,
                    chat_type=inferred_type,
                ):
                    migrated += 1

            logger.info(f"Migrated {migrated} chats from seen_chats.json")
            return migrated

        except Exception as e:
            logger.warning(f"Failed to migrate seen_chats.json: {e}")
            return 0

    def sync_from_bridge_metadata(
        self,
        channel: str,
        bridge_metadata_list: list[dict[str, Any]],
    ) -> dict[str, bool]:
        """Sync chat metadata from bridge response.

        Args:
            channel: Channel name (e.g., 'whatsapp')
            bridge_metadata_list: List of metadata dicts from bridge

        Returns:
            Dict mapping chat_id to whether it was newly registered (True) or updated (False)
        """
        results: dict[str, bool] = {}

        for meta in bridge_metadata_list:
            if not isinstance(meta, dict):
                continue

            chat_jid = meta.get("chatJid")
            if not chat_jid:
                continue

            # Determine chat type from JID pattern
            if chat_jid.endswith("@g.us"):
                chat_type = ChatType.GROUP.value
            elif chat_jid.endswith("@lid") or ".s.whatsapp.net" in chat_jid:
                chat_type = ChatType.DM.value
            else:
                chat_type = ChatType.UNKNOWN.value

            # Extract bridge metadata fields
            is_new = self.register_chat(
                channel=channel,
                chat_id=chat_jid,
                chat_type=chat_type,
                readable_name=meta.get("subject"),
                description=meta.get("desc"),
                owner_id=meta.get("owner"),
                participant_count=meta.get("size"),
                is_community=meta.get("isCommunity"),
                invite_code=meta.get("inviteCode"),
                metadata={
                    "subjectOwner": meta.get("subjectOwner"),
                    "subjectTime": meta.get("subjectTime"),
                    "descOwner": meta.get("descOwner"),
                    "descTime": meta.get("descTime"),
                    "descId": meta.get("descId"),
                    "creation": meta.get("creation"),
                    "isParentGroup": meta.get("isParentGroup"),
                    "isAnnounceGrpRestrict": meta.get("isAnnounceGrpRestrict"),
                    "isMemberGroup": meta.get("isMemberGroup"),
                    "restrict": meta.get("restrict"),
                    "announce": meta.get("announce"),
                    "ephemeralDuration": meta.get("ephemeralDuration"),
                    "ephemeralSettingTimestamp": meta.get("ephemeralSettingTimestamp"),
                    "defaultInviteExpiration": meta.get("defaultInviteExpiration"),
                    "inviteLinkPreventJoin": meta.get("inviteLinkPreventJoin"),
                    "participantAdInfo": meta.get("participantAdInfo"),
                    "groupSet": meta.get("groupSet"),
                    "groupTypes": meta.get("groupTypes"),
                    "linkedParent": meta.get("linkedParent"),
                    "groupMetadata": meta.get("groupMetadata"),
                    "participants": meta.get("participants"),
                },
            )

            results[chat_jid] = is_new

        if results:
            logger.info(f"Synced {len(results)} chats from {channel} bridge")

        return results

    def close(self) -> None:
        """Close sqlite connection."""
        with self._lock:
            self._conn.close()
