"""Long-term memory service: recall, capture, backfill, and hygiene."""

from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from nanobot.memory.backends.base import MemoryBackend
from nanobot.memory.backends.sqlite_fts import SqliteFtsMemoryBackend
from nanobot.memory.extractor import extract_candidates
from nanobot.memory.models import (
    MemoryCaptureCandidate,
    MemoryCaptureResult,
    MemoryEntry,
    MemoryHit,
    MemoryKind,
    MemoryScopeType,
)
from nanobot.memory.render import render_memory_hits
from nanobot.memory.session_state import SessionStateStore
from nanobot.utils.helpers import ensure_dir

if TYPE_CHECKING:
    from nanobot.config.schema import MemoryConfig


class MemoryService:
    """Coordinates long-term memory retrieval and persistence."""

    BACKFILL_MARKER = "workspace:{workspace_id}:backfill:v1"

    def __init__(
        self,
        *,
        workspace: Path,
        config: "MemoryConfig",
        backend: MemoryBackend | None = None,
    ) -> None:
        self.workspace = workspace
        self.config = config
        self.workspace_id = hashlib.sha1(
            str(workspace.expanduser().resolve()).encode("utf-8")
        ).hexdigest()[:16]

        db_path = Path(config.db_path).expanduser()
        if not db_path.is_absolute():
            db_path = (Path.home() / ".nanobot" / db_path).resolve()

        backend_id = str(getattr(config, "backend", "sqlite_fts")).strip().lower()
        if backend is not None:
            self.backend = backend
        elif backend_id == "sqlite_fts":
            self.backend = SqliteFtsMemoryBackend(db_path)
        elif backend_id == "reserved_hybrid":
            logger.info("memory backend '{}' not implemented yet; using sqlite_fts", backend_id)
            self.backend = SqliteFtsMemoryBackend(db_path)
        else:
            logger.warning("unknown memory backend '{}'; using sqlite_fts", backend_id)
            self.backend = SqliteFtsMemoryBackend(db_path)
        self.state_store = SessionStateStore(workspace, state_dir=config.wal.state_dir)
        self._last_hygiene = 0.0

    @staticmethod
    def chat_scope_key(channel: str, chat_id: str) -> str:
        return f"channel:{channel}:chat:{chat_id}"

    @staticmethod
    def user_scope_key(channel: str, sender_id_or_chat_id: str) -> str:
        return f"channel:{channel}:user:{sender_id_or_chat_id}"

    def global_scope_key(self) -> str:
        return f"workspace:{self.workspace_id}:global"

    def _scope_for_kind(
        self,
        *,
        kind: MemoryKind,
        channel: str,
        chat_id: str,
        sender_id: str | None,
    ) -> tuple[MemoryScopeType, str]:
        if kind in {"preference", "fact"}:
            owner = (sender_id or chat_id).strip()
            return "user", self.user_scope_key(channel, owner)
        if kind in {"decision", "episodic"}:
            return "chat", self.chat_scope_key(channel, chat_id)
        return "global", self.global_scope_key()

    @staticmethod
    def _normalize_content(content: str) -> str:
        return " ".join(content.split()).strip()

    @classmethod
    def _hash_content(cls, content: str) -> str:
        return hashlib.sha256(cls._normalize_content(content).lower().encode("utf-8")).hexdigest()

    @staticmethod
    def _parse_iso(iso_text: str) -> datetime:
        dt = datetime.fromisoformat(iso_text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)

    @staticmethod
    def _recency_score(updated_at: str) -> float:
        try:
            dt = MemoryService._parse_iso(updated_at)
        except ValueError:
            return 0.0
        age_days = max(0.0, (datetime.now(UTC) - dt).total_seconds() / 86400.0)
        return math.exp(-age_days / 180.0)

    @staticmethod
    def _normalize_fts_scores(hits: list[MemoryHit]) -> None:
        if not hits:
            return
        raw_scores = [hit.fts_score for hit in hits]
        min_raw = min(raw_scores)
        max_raw = max(raw_scores)

        for hit in hits:
            if abs(max_raw - min_raw) < 1e-9:
                hit.fts_score_norm = 1.0
            else:
                # bm25: lower is better -> invert range.
                hit.fts_score_norm = (max_raw - hit.fts_score) / (max_raw - min_raw)
            hit.fts_score_norm = max(0.0, min(1.0, hit.fts_score_norm))

    @staticmethod
    def _finalize_ranking(hits: list[MemoryHit]) -> list[MemoryHit]:
        MemoryService._normalize_fts_scores(hits)
        for hit in hits:
            hit.recency_score = MemoryService._recency_score(hit.entry.updated_at)
            hit.final_score = (
                0.65 * hit.fts_score_norm
                + 0.20 * float(hit.entry.importance)
                + 0.15 * hit.recency_score
            )
        return sorted(hits, key=lambda h: h.final_score, reverse=True)

    def recall_for_event(
        self,
        *,
        channel: str,
        chat_id: str,
        sender_id: str | None,
        query: str,
        reply_to_text: str | None = None,
    ) -> list[MemoryHit]:
        if not self.config.enabled:
            return []

        query_text = self._normalize_content(
            query + (f"\n{reply_to_text}" if reply_to_text else "")
        )
        if not query_text:
            return []

        chat_scope = self.chat_scope_key(channel, chat_id)
        user_scope = self.user_scope_key(channel, (sender_id or chat_id).strip())

        max_results = max(1, int(self.config.recall.max_results))
        user_layer_results = max(0, int(self.config.recall.user_preference_layer_results))

        chat_hits = self.backend.search(
            workspace_id=self.workspace_id,
            query=query_text,
            scope_keys=[chat_scope],
            kinds=None,
            limit=max_results,
        )
        user_hits = self.backend.search(
            workspace_id=self.workspace_id,
            query=query_text,
            scope_keys=[user_scope],
            kinds={"preference", "fact"},
            limit=user_layer_results,
        )

        merged: dict[str, MemoryHit] = {}
        for hit in [*chat_hits, *user_hits]:
            existing = merged.get(hit.entry.id)
            if existing is None or hit.fts_score < existing.fts_score:
                merged[hit.entry.id] = hit

        ranked = self._finalize_ranking(list(merged.values()))
        return ranked[:max_results]

    def build_retrieved_context(
        self,
        *,
        channel: str,
        chat_id: str,
        sender_id: str | None,
        query: str,
        reply_to_text: str | None = None,
    ) -> tuple[str, list[MemoryHit]]:
        hits = self.recall_for_event(
            channel=channel,
            chat_id=chat_id,
            sender_id=sender_id,
            query=query,
            reply_to_text=reply_to_text,
        )
        text = render_memory_hits(hits, max_chars=int(self.config.recall.max_prompt_chars))
        return text, hits

    def pre_write_session_state(
        self,
        *,
        session_key: str,
        channel: str,
        chat_id: str,
        user_message: str,
        metadata: dict[str, object],
    ) -> None:
        if not (self.config.enabled and self.config.wal.enabled):
            return
        self.state_store.pre_write(
            session_key=session_key,
            channel=channel,
            chat_id=chat_id,
            user_message=user_message,
            metadata=metadata,
        )

    def post_write_session_state(
        self,
        *,
        session_key: str,
        assistant_reply: str,
        pending_actions: list[str] | None = None,
    ) -> None:
        if not (self.config.enabled and self.config.wal.enabled):
            return
        self.state_store.post_write(
            session_key=session_key,
            assistant_reply=assistant_reply,
            pending_actions=pending_actions,
        )

    def _expires_at_for_kind(self, kind: MemoryKind) -> str | None:
        now = datetime.now(UTC)
        if kind == "episodic":
            days = int(self.config.retention.episodic_days)
        elif kind == "fact":
            days = int(self.config.retention.fact_days)
        elif kind == "preference":
            days = int(self.config.retention.preference_days)
        elif kind == "decision":
            days = int(self.config.retention.decision_days)
        else:
            return None

        if days <= 0:
            return None
        return (now + timedelta(days=days)).isoformat()

    def _entry_from_candidate(
        self,
        *,
        channel: str,
        chat_id: str,
        sender_id: str | None,
        candidate: MemoryCaptureCandidate,
        source: str,
    ) -> MemoryEntry:
        scope_type, scope_key = self._scope_for_kind(
            kind=candidate.kind,
            channel=channel,
            chat_id=chat_id,
            sender_id=sender_id,
        )
        compact = self._normalize_content(candidate.content)
        now_iso = datetime.now(UTC).isoformat()
        return MemoryEntry(
            id="",
            workspace_id=self.workspace_id,
            scope_type=scope_type,
            scope_key=scope_key,
            channel=channel,
            chat_id=chat_id,
            sender_id=sender_id,
            kind=candidate.kind,
            content=compact,
            content_norm=compact.lower(),
            content_hash=self._hash_content(compact),
            importance=float(candidate.importance),
            confidence=float(candidate.confidence),
            source=source,
            source_message_id=candidate.source_message_id,
            source_role=candidate.source_role,
            meta_json=json.dumps(candidate.metadata, ensure_ascii=False),
            created_at=now_iso,
            updated_at=now_iso,
            expires_at=self._expires_at_for_kind(candidate.kind),
        )

    def _mirror_entry_to_files(self, entry: MemoryEntry) -> None:
        memory_root = ensure_dir(self.workspace / "memory")
        now_label = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")

        if entry.kind == "episodic":
            episodic_dir = ensure_dir(memory_root / "episodic")
            day_file = episodic_dir / f"{datetime.now(UTC).strftime('%Y-%m-%d')}.md"
            if not day_file.exists():
                day_file.write_text(
                    f"# {datetime.now(UTC).strftime('%Y-%m-%d')}\n\n",
                    encoding="utf-8",
                )
            with day_file.open("a", encoding="utf-8") as f:
                f.write(f"- [{now_label}] {entry.content}\n")
            return

        semantic_dir = ensure_dir(memory_root / "semantic")
        filename = {
            "preference": "preferences.md",
            "fact": "facts.md",
            "decision": "decisions.md",
        }.get(entry.kind, "facts.md")
        target = semantic_dir / filename
        if not target.exists():
            target.write_text(f"# {filename[:-3].replace('-', ' ').title()}\n\n", encoding="utf-8")
        with target.open("a", encoding="utf-8") as f:
            f.write(f"- [{now_label}] {entry.content}\n")

    def capture_from_turn(
        self,
        *,
        channel: str,
        chat_id: str,
        sender_id: str | None,
        user_message: str,
        source_message_id: str | None,
        assistant_reply: str | None = None,
    ) -> MemoryCaptureResult:
        result = MemoryCaptureResult()
        if not self.config.enabled:
            return result
        if not self.config.capture.enabled:
            return result
        if channel not in set(self.config.capture.channels):
            return result

        candidates, dropped_safety = extract_candidates(
            user_message,
            source_message_id=source_message_id,
            source_role="user",
            include_episodic=True,
        )
        result.dropped_safety += dropped_safety

        if assistant_reply and bool(self.config.capture.capture_assistant):
            assistant_candidates, assistant_dropped = extract_candidates(
                assistant_reply,
                source_message_id=source_message_id,
                source_role="assistant",
                include_episodic=False,
            )
            candidates.extend(assistant_candidates)
            result.dropped_safety += assistant_dropped

        result.candidates = candidates
        min_conf = float(self.config.capture.min_confidence)
        min_importance = float(self.config.capture.min_importance)
        max_per_turn = int(self.config.capture.max_entries_per_turn)

        accepted = 0
        for candidate in candidates:
            if candidate.confidence < min_conf:
                result.dropped_low_confidence += 1
                continue
            if candidate.importance < min_importance:
                result.dropped_low_importance += 1
                continue
            if accepted >= max_per_turn:
                break

            entry = self._entry_from_candidate(
                channel=channel,
                chat_id=chat_id,
                sender_id=sender_id,
                candidate=candidate,
                source="auto_heuristic",
            )
            saved_entry, inserted = self.backend.upsert_entry(entry)
            if not inserted:
                result.deduped += 1
            else:
                result.saved.append(saved_entry)
                self._mirror_entry_to_files(saved_entry)
            accepted += 1

        self._maybe_hygiene()
        return result

    def record_manual(
        self,
        *,
        channel: str,
        chat_id: str,
        sender_id: str | None,
        scope_type: MemoryScopeType,
        kind: MemoryKind,
        text: str,
        importance: float,
        confidence: float = 1.0,
        source_message_id: str | None = None,
    ) -> tuple[MemoryEntry, bool]:
        compact = self._normalize_content(text)
        if scope_type == "chat":
            scope_key = self.chat_scope_key(channel, chat_id)
        elif scope_type == "user":
            scope_key = self.user_scope_key(channel, (sender_id or chat_id).strip())
        else:
            scope_key = self.global_scope_key()

        now_iso = datetime.now(UTC).isoformat()
        entry = MemoryEntry(
            id="",
            workspace_id=self.workspace_id,
            scope_type=scope_type,
            scope_key=scope_key,
            channel=channel,
            chat_id=chat_id,
            sender_id=sender_id,
            kind=kind,
            content=compact,
            content_norm=compact.lower(),
            content_hash=self._hash_content(compact),
            importance=float(importance),
            confidence=float(confidence),
            source="manual",
            source_message_id=source_message_id,
            source_role="user",
            created_at=now_iso,
            updated_at=now_iso,
            expires_at=self._expires_at_for_kind(kind),
        )
        saved_entry, inserted = self.backend.upsert_entry(entry)
        if inserted:
            self._mirror_entry_to_files(saved_entry)
        return saved_entry, inserted

    def search(
        self,
        *,
        query: str,
        channel: str | None = None,
        chat_id: str | None = None,
        sender_id: str | None = None,
        scope: str = "all",
        limit: int = 8,
    ) -> list[MemoryHit]:
        scope_keys: list[str] = []
        if scope in {"chat", "all"} and channel and chat_id:
            scope_keys.append(self.chat_scope_key(channel, chat_id))
        if scope in {"user", "all"} and channel and (sender_id or chat_id):
            scope_keys.append(self.user_scope_key(channel, (sender_id or chat_id).strip()))
        if scope in {"global", "all"}:
            scope_keys.append(self.global_scope_key())

        if not scope_keys:
            scope_keys.append(self.global_scope_key())

        hits = self.backend.search(
            workspace_id=self.workspace_id,
            query=query,
            scope_keys=scope_keys,
            kinds=None,
            limit=max(1, int(limit)),
        )
        return self._finalize_ranking(hits)

    def list_entries(
        self,
        *,
        scope_keys: list[str] | None = None,
        kinds: set[MemoryKind] | None = None,
        limit: int = 100,
        include_deleted: bool = False,
    ) -> list[MemoryEntry]:
        return self.backend.list_entries(
            workspace_id=self.workspace_id,
            scope_keys=scope_keys,
            kinds=kinds,
            limit=limit,
            include_deleted=include_deleted,
        )

    def prune(
        self,
        *,
        older_than_days: int | None = None,
        kinds: set[MemoryKind] | None = None,
        scope_keys: list[str] | None = None,
        dry_run: bool = False,
    ) -> int:
        older_than: datetime | None = None
        if older_than_days is not None:
            older_than = datetime.now(UTC) - timedelta(days=max(0, int(older_than_days)))
        return self.backend.prune(
            workspace_id=self.workspace_id,
            older_than=older_than,
            kinds=kinds,
            scope_keys=scope_keys,
            dry_run=dry_run,
        )

    def reindex(self) -> None:
        self.backend.reindex()

    def stats(self) -> dict[str, object]:
        stats = self.backend.stats(workspace_id=self.workspace_id)
        stats["enabled"] = bool(self.config.enabled)
        stats["backend"] = str(getattr(self.config, "backend", "sqlite_fts"))
        stats["wal_enabled"] = bool(self.config.wal.enabled)
        stats["state_dir"] = str(self.state_store.state_dir)
        marker = self.backend.get_meta(self.BACKFILL_MARKER.format(workspace_id=self.workspace_id))
        stats["backfill_marker"] = marker or ""
        state_files = list(self.state_store.state_dir.glob("*.md"))
        stats["wal_files"] = len(state_files)
        return stats

    def backfill_from_workspace_files(self, *, force: bool = False) -> int:
        marker_key = self.BACKFILL_MARKER.format(workspace_id=self.workspace_id)
        if not force and self.backend.get_meta(marker_key) == "done":
            return 0

        imported = 0
        memory_root = ensure_dir(self.workspace / "memory")

        def import_file(path: Path, kind: MemoryKind) -> None:
            nonlocal imported
            if not path.exists() or not path.is_file():
                return
            raw = path.read_text(encoding="utf-8").strip()
            if not raw:
                return
            compact = self._normalize_content(raw)
            if len(compact) < 12:
                return
            if len(compact) > 500:
                compact = compact[:500] + "..."

            now_iso = datetime.now(UTC).isoformat()
            entry = MemoryEntry(
                id="",
                workspace_id=self.workspace_id,
                scope_type="global",
                scope_key=self.global_scope_key(),
                kind=kind,
                content=compact,
                content_norm=compact.lower(),
                content_hash=self._hash_content(compact),
                importance=0.7,
                confidence=0.85,
                source="import",
                source_role="user",
                created_at=now_iso,
                updated_at=now_iso,
                meta_json=json.dumps({"file": str(path)}, ensure_ascii=False),
                expires_at=self._expires_at_for_kind(kind),
            )
            _, inserted = self.backend.upsert_entry(entry)
            if inserted:
                imported += 1

        import_file(memory_root / "MEMORY.md", "fact")

        for daily in sorted(memory_root.glob("????-??-??.md")):
            import_file(daily, "episodic")

        for episodic in sorted((memory_root / "episodic").glob("*.md")):
            import_file(episodic, "episodic")

        for semantic in sorted((memory_root / "semantic").glob("*.md")):
            stem = semantic.stem.lower()
            kind: MemoryKind = "fact"
            if "preference" in stem:
                kind = "preference"
            elif "decision" in stem:
                kind = "decision"
            import_file(semantic, kind)

        for procedural in sorted((memory_root / "procedural").glob("*.md")):
            import_file(procedural, "decision")

        self.backend.set_meta(marker_key, "done")
        return imported

    def _maybe_hygiene(self) -> None:
        now = time.monotonic()
        if now - self._last_hygiene < 3600:
            return
        self._last_hygiene = now
        try:
            self.backend.prune_expired(workspace_id=self.workspace_id, dry_run=False)
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.warning("memory hygiene prune_expired failed: {}", exc)

    def close(self) -> None:
        close_fn = getattr(self.backend, "close", None)
        if callable(close_fn):
            close_fn()
