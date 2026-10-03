"""Fail-closed search and recent reads over an isolated rebuilt history journal."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from collections.abc import Mapping
from typing import Any

from yeoman_gateway.knowledge.models import (
    KnowledgeContext,
    TrustedReadContext,
    ValidationError,
)
from yeoman_gateway.knowledge.runtime import RuntimeKnowledgePolicy

from ._history import HistoricalJournal, HistorySourceAuthority
from ._history_audience import AudienceProof, HistoryAudience

_FTS_TABLE = "history_search_fts"
_DENIED_RETENTION = frozenset({"deleted", "erased", "purged", "suppressed", "revoked"})
_NATIVE_SOURCE_AUTHORITIES = frozenset(
    {"native_payload", "native_envelope", "inbound_archive_copy", "source_copy", "journal_event"}
)
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
_DIRECT_KINDS = frozenset({"direct", "dm", "private"})


class HistorySearchUnsupportedError(RuntimeError):
    """SQLite does not provide FTS5 for this offline journal."""


class HistoryReader:
    """Read authorized original events from one already rebuilt journal."""

    def __init__(self, journal: HistoricalJournal, *, policy: RuntimeKnowledgePolicy) -> None:
        if not isinstance(journal, HistoricalJournal):
            raise TypeError("journal must be a HistoricalJournal")
        self.journal = journal
        self.policy = policy
        self.audience = HistoryAudience(journal)

    def reindex(self) -> dict[str, Any]:
        """Rebuild the owned FTS projection from canonical normalized journal rows."""
        if not self.journal.rebuild_state().get("complete"):
            return {"status": "incomplete", "supported": True, "indexed": 0}
        indexed = 0
        try:
            with self.journal.store._write() as connection:
                connection.execute(
                    f"CREATE VIRTUAL TABLE IF NOT EXISTS {_FTS_TABLE} USING fts5("
                    "copy_id UNINDEXED,event_id UNINDEXED,revision UNINDEXED,"
                    "source_id UNINDEXED,locator_json UNINDEXED,source_kind UNINDEXED,"
                    "provenance_class UNINDEXED,source_authority UNINDEXED,native_id UNINDEXED,"
                    "text_hash UNINDEXED,normalized_hash UNINDEXED,normalized_text)"
                )
                connection.execute(f"DELETE FROM {_FTS_TABLE}")
                details = connection.execute(
                    "SELECT event_id,revision,normalized_json,retention_status,text_hash,denied "
                    "FROM history_event_details ORDER BY event_id,revision"
                ).fetchall()
                for detail in details:
                    event_id, revision = str(detail["event_id"]), str(detail["revision"])
                    event = _decode_object(detail["normalized_json"])
                    if event is None or not self._indexable_detail(detail, event):
                        continue
                    text = event.get("text")
                    digest = event.get("text_hash")
                    if not isinstance(text, str) or not text.strip() or not _valid_hash(text, digest):
                        continue
                    normalized = _normalize_text(text)
                    if not normalized:
                        continue
                    copies = self._copies(event_id, revision)
                    if self._event_denied(event_id, revision, event):
                        continue
                    for copy in copies:
                        if not self._copy_matches(copy, event, require_text=True):
                            continue
                        connection.execute(
                            f"INSERT INTO {_FTS_TABLE} (copy_id,event_id,revision,source_id,"
                            "locator_json,source_kind,provenance_class,source_authority,native_id,"
                            "text_hash,normalized_hash,normalized_text) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                int(copy["copy_id"]),
                                event_id,
                                revision,
                                str(copy["source_id"]),
                                str(copy["locator_json"]),
                                str(copy["source_kind"]),
                                str(copy["provenance_class"]),
                                str(copy["source_authority"] or ""),
                                str(copy["native_id"] or ""),
                                str(copy["text_hash"]),
                                hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
                                normalized,
                            ),
                        )
                        indexed += 1
        except sqlite3.OperationalError as exc:
            if "fts5" in str(exc).casefold() or "no such module" in str(exc).casefold():
                return {
                    "status": "unsupported",
                    "supported": False,
                    "capability": "sqlite_fts5",
                    "indexed": 0,
                }
            raise
        return {"status": "ok", "supported": True, "indexed": indexed}

    def search(
        self,
        query: str,
        *,
        context: TrustedReadContext,
        channel: str,
        account: str,
        chat_id: str,
        since_ms: int | None = None,
        until_ms: int | None = None,
        native_id: str | None = None,
        limit: int = 30,
    ) -> tuple[dict[str, Any], ...]:
        """Search lexical FTS candidates and apply current authorization per result."""
        scope = self._scope(context, channel, account, chat_id)
        self._validate_limit(limit)
        if not isinstance(query, str):
            raise ValueError("query must be text")
        tokens = _TOKEN.findall(unicodedata.normalize("NFKC", query).casefold())
        if not scope or not tokens or not self._authorized(context):
            return ()
        if since_ms is not None and not _timestamp(since_ms):
            raise ValueError("since_ms must be a positive integer timestamp")
        if until_ms is not None and not _timestamp(until_ms):
            raise ValueError("until_ms must be a positive integer timestamp")
        if since_ms is not None and until_ms is not None and since_ms >= until_ms:
            return ()
        if not self._fts_exists():
            rebuilt = self.reindex()
            if not rebuilt["supported"]:
                raise HistorySearchUnsupportedError("SQLite FTS5 is unavailable")
        expression = " AND ".join('"' + token.replace('"', '""') + '"' for token in tokens)
        with self.journal.store._lock:
            try:
                candidates = self.journal.store._conn.execute(
                    f"SELECT DISTINCT event_id,revision FROM {_FTS_TABLE} "
                    "WHERE normalized_text MATCH ? ORDER BY event_id,revision",
                    (expression,),
                ).fetchall()
            except sqlite3.OperationalError as exc:
                if "fts5" in str(exc).casefold() or "no such module" in str(exc).casefold():
                    raise HistorySearchUnsupportedError("SQLite FTS5 is unavailable") from exc
                raise
        results: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for row in candidates:
            key = (str(row["event_id"]), str(row["revision"]))
            if key in seen:
                continue
            item = self._event_view(*key, scope=scope, context=context)
            if item is None:
                with self.journal.store._lock:
                    detail = self.journal.store._conn.execute(
                        "SELECT normalized_json FROM history_event_details WHERE event_id=? AND revision=?",
                        key,
                    ).fetchone()
                event = None if detail is None else _decode_object(detail["normalized_json"])
                if event is not None and self._event_denied(*key, event):
                    self._remove_indexed_event(*key)
                continue
            timestamp = _query_time(item)
            if since_ms is not None and (timestamp is None or timestamp < since_ms):
                continue
            if until_ms is not None and (timestamp is None or timestamp >= until_ms):
                continue
            if native_id is not None and item["native_id"] != native_id:
                continue
            seen.add(key)
            results.append(item)
        results.sort(key=_sort_key, reverse=True)
        return self._finalize_events(results, context=context)[:limit]

    def recent(
        self,
        *,
        context: TrustedReadContext,
        channel: str,
        account: str,
        chat_id: str,
        before_ms: int,
        limit: int = 8,
    ) -> tuple[dict[str, Any], ...]:
        """Return the newest authorized canonical events in one exact account scope."""
        scope = self._scope(context, channel, account, chat_id)
        self._validate_limit(limit)
        if not scope or not _timestamp(before_ms) or not self._authorized(context):
            return ()
        with self.journal.store._lock:
            rows = self.journal.store._conn.execute(
                "SELECT event_id,revision FROM history_event_details ORDER BY event_id,revision"
            ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            item = self._event_view(str(row["event_id"]), str(row["revision"]), scope=scope, context=context)
            moment = None if item is None else _query_time(item)
            if item is not None and moment is not None and moment < before_ms:
                results.append(item)
        results.sort(key=_sort_key, reverse=True)
        return self._finalize_events(results, context=context)[:limit]

    def excerpt(
        self,
        event_id: str,
        revision: int,
        *,
        context: TrustedReadContext,
    ) -> dict[str, Any] | None:
        """Resolve one exact canonical ID or unambiguous source alias."""
        if not self._authorized(context) or not isinstance(event_id, str) or not event_id:
            return None
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            return None
        with self.journal.store._lock:
            aliases = self.journal.store._conn.execute(
                "SELECT DISTINCT a.canonical_event_id,a.canonical_revision,c.channel,c.account,"
                "c.chat_id,c.native_id FROM history_event_aliases a "
                "LEFT JOIN history_event_copies c ON c.event_id=a.canonical_event_id "
                "AND c.revision=a.canonical_revision AND c.source_id=a.source_id "
                "AND c.locator_json=a.locator_json WHERE a.source_event_id=? "
                "AND a.source_revision=?",
                (event_id, str(revision)),
            ).fetchall()
            if aliases:
                targets = {
                    (str(row["canonical_event_id"]), str(row["canonical_revision"]))
                    for row in aliases
                }
                scopes = {
                    (row["channel"], row["account"], row["chat_id"], row["native_id"])
                    for row in aliases
                }
                if len(targets) != 1 or len(scopes) != 1:
                    return None
                canonical_id, canonical_revision = next(iter(targets))
                channel, account, chat_id, _native_id = next(iter(scopes))
            else:
                canonical_id, canonical_revision = event_id, str(revision)
                rows = self.journal.store._conn.execute(
                    "SELECT DISTINCT channel,account,chat_id,native_id FROM history_event_copies "
                    "WHERE event_id=? AND revision=?",
                    (canonical_id, canonical_revision),
                ).fetchall()
                scopes = {
                    (row["channel"], row["account"], row["chat_id"], row["native_id"])
                    for row in rows
                }
                if len(scopes) != 1:
                    return None
                channel, account, chat_id, _native_id = next(iter(scopes))
        if channel != context.channel or chat_id != context.chat_id:
            return None
        if not all(isinstance(item, str) and item for item in (channel, account, chat_id)):
            return None
        requested_scope = (str(channel), str(account), str(chat_id))
        result = self._event_view(
            canonical_id, canonical_revision, scope=requested_scope, context=context
        )
        if result is not None:
            result["requested_event_id"] = event_id
            result["requested_revision"] = revision
            checked = self._finalize_events((result,), context=context)
            return checked[0] if checked else None
        return None

    def revalidate(
        self,
        result: KnowledgeContext,
        *,
        knowledge: Any,
        context: TrustedReadContext,
    ) -> KnowledgeContext:
        """Re-run the real KnowledgeService gate, then check its current source proofs."""
        if not isinstance(result, KnowledgeContext):
            raise ValidationError("result must be a KnowledgeContext")
        if not self._authorized(context):
            return _empty_context(result, "read_denied")
        checked = knowledge.revalidate(result, context=context)
        if not isinstance(checked, KnowledgeContext):
            raise ValidationError("knowledge.revalidate must return a KnowledgeContext")
        if checked.empty:
            return checked
        authority = HistorySourceAuthority(self.journal)
        with self.journal.store._lock:
            if not self._authorized(context):
                return _empty_context(checked, "read_changed_during_revalidation")
            epoch = self._store_epoch()
            if not self._knowledge_sources_allowed(
                checked, context=context, authority=authority
            ):
                return _empty_context(checked, "source_revoked_or_unverified")
            if epoch != self._store_epoch():
                return _empty_context(checked, "read_changed_during_revalidation")
        return checked

    def _knowledge_sources_allowed(
        self,
        result: KnowledgeContext,
        *,
        context: TrustedReadContext,
        authority: HistorySourceAuthority,
    ) -> bool:
        recipients = context.recipient_principals or frozenset()
        for source in result.source_refs:
            if (
                source.channel != context.channel
                or source.chat_id != context.chat_id
                or not authority.verify_source(source)
                or authority.source_revoked(source)
            ):
                return False
            audience = authority.evidence_audience(source, basis="history")
            if audience is None or audience.status == "unknown":
                return False
            if audience.status == "author_only":
                if not context.is_direct or recipients != frozenset({source.author_principal}):
                    return False
            elif not recipients or not recipients.issubset(audience.members):
                return False
        return True

    def _event_view(
        self,
        event_id: str,
        revision: str,
        *,
        scope: tuple[str, str, str],
        context: TrustedReadContext,
    ) -> dict[str, Any] | None:
        if not self._authorized(context):
            return None
        with self.journal.store._lock:
            row = self.journal.store._conn.execute(
                "SELECT normalized_json,retention_status,denied,text_hash "
                "FROM history_event_details WHERE event_id=? AND revision=?",
                (event_id, revision),
            ).fetchone()
        if row is None:
            return None
        event = _decode_object(row["normalized_json"])
        if event is None or not self._indexable_detail(row, event):
            return None
        if (event.get("channel"), event.get("account"), event.get("chat_id")) != scope:
            return None
        if self._event_denied(event_id, revision, event):
            return None
        copies = [
            copy for copy in self._copies(event_id, revision)
            if self._copy_matches(copy, event, require_text=False)
            and (copy.get("channel"), copy.get("account"), copy.get("chat_id")) == scope
        ]
        if not copies:
            return None
        proof = self.audience.resolve(event)
        if not self._audience_allows(proof, event, context):
            return None
        if not self._authorized(context):
            return None
        normalized_text = event.get("text")
        if normalized_text is not None and (
            not isinstance(normalized_text, str)
            or not _valid_hash(normalized_text, event.get("text_hash"))
        ):
            return None
        return {
            "event_id": event_id,
            "revision": _revision_value(revision),
            "requested_event_id": event_id,
            "requested_revision": _revision_value(revision),
            "channel": str(event["channel"]),
            "account": str(event["account"]),
            "chat_id": str(event["chat_id"]),
            "native_id": _optional_text(event.get("native_id")),
            "kind": _optional_text(event.get("kind")),
            "direction": _optional_text(event.get("direction")),
            "sender_raw": _optional_text(event.get("sender_raw")),
            "principal": _optional_text(event.get("principal")),
            "observed_ms": _timestamp_value(event.get("observed_ms")),
            "occurred_ms": (
                _timestamp_value(event.get("occurred_ms"))
                if _time_basis(event) == "occurred"
                else None
            ),
            "time_basis": _time_basis(event),
            "time_certainty": _time_certainty(event),
            "normalization_version": _revision_value(event.get("normalization_version")),
            "text": normalized_text,
            "text_hash": _optional_text(event.get("text_hash")),
            "media_kind": _optional_text(event.get("media_kind")),
            "media_missing": bool(event.get("media_missing")),
            "reply_target": _safe_reference(event.get("reply_target")),
            "edit_target": _safe_reference(event.get("edit_target")),
            "delete_target": _safe_reference(event.get("delete_target")),
            "provenance_class": str(event.get("provenance_class") or "unknown"),
            "source_authority": str(event.get("source_authority") or "unknown"),
            "verbatim_unverified": bool(event.get("verbatim_unverified")),
            "audience": {
                "status": proof.status,
                "evidence_class": proof.evidence_class,
                "proof_id": proof.proof_id,
            },
            "sources": [self._source_view(copy) for copy in copies],
        }

    def _finalize_events(
        self,
        results: tuple[dict[str, Any], ...] | list[dict[str, Any]],
        *,
        context: TrustedReadContext,
    ) -> tuple[dict[str, Any], ...]:
        """Recheck every candidate after the final current-membership callback."""
        if not results:
            return ()
        with self.journal.store._lock:
            if not self._authorized(context):
                return ()
            epoch = self._store_epoch()
            finalized: list[dict[str, Any]] = []
            for result in results:
                event_id = result.get("event_id")
                revision = result.get("revision")
                scope = (
                    result.get("channel"),
                    result.get("account"),
                    result.get("chat_id"),
                )
                if (
                    not isinstance(event_id, str)
                    or isinstance(revision, bool)
                    or not isinstance(revision, int)
                    or any(not isinstance(item, str) or not item for item in scope)
                ):
                    continue
                state = self._current_event_state(
                    event_id,
                    str(revision),
                    scope=(scope[0], scope[1], scope[2]),
                    context=context,
                )
                if state is None:
                    continue
                event, copies, proof = state
                if (
                    result.get("text") != event.get("text")
                    or result.get("text_hash") != event.get("text_hash")
                ):
                    continue
                result["audience"] = {
                    "status": proof.status,
                    "evidence_class": proof.evidence_class,
                    "proof_id": proof.proof_id,
                }
                result["sources"] = [self._source_view(copy) for copy in copies]
                finalized.append(result)
            if epoch != self._store_epoch():
                return ()
            return tuple(finalized)

    def _current_event_state(
        self,
        event_id: str,
        revision: str,
        *,
        scope: tuple[str, str, str],
        context: TrustedReadContext,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], AudienceProof] | None:
        row = self.journal.store._conn.execute(
            "SELECT normalized_json,retention_status,denied,text_hash "
            "FROM history_event_details WHERE event_id=? AND revision=?",
            (event_id, revision),
        ).fetchone()
        if row is None:
            return None
        event = _decode_object(row["normalized_json"])
        if (
            event is None
            or not self._indexable_detail(row, event)
            or (event.get("channel"), event.get("account"), event.get("chat_id")) != scope
            or self._event_denied(event_id, revision, event)
        ):
            return None
        text = event.get("text")
        if text is not None and (
            not isinstance(text, str) or not _valid_hash(text, event.get("text_hash"))
        ):
            return None
        copies = [
            copy for copy in self._copies(event_id, revision)
            if self._copy_matches(copy, event, require_text=False)
            and (copy.get("channel"), copy.get("account"), copy.get("chat_id")) == scope
        ]
        if not copies:
            return None
        proof = self.audience.resolve(event)
        if not self._audience_allows(proof, event, context):
            return None
        return event, copies, proof

    @staticmethod
    def _source_view(copy: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "source_id": str(copy["source_id"]),
            "source_hash": _optional_text(copy.get("source_hash")),
            "source_kind": str(copy["source_kind"]),
            "provenance_class": str(copy["provenance_class"]),
            "source_authority": _optional_text(copy.get("source_authority")),
            "text_hash": _optional_text(copy.get("text_hash")),
            "locator": _decode_object(copy["locator_json"]),
        }

    def _store_epoch(self) -> tuple[int, int]:
        connection = self.journal.store._conn
        version = connection.execute("PRAGMA data_version").fetchone()[0]
        return int(version), int(connection.total_changes)

    def _indexable_detail(self, detail: Mapping[str, Any], event: Mapping[str, Any]) -> bool:
        if int(detail["denied"] or 0) != 0 or str(detail["retention_status"]).casefold() in _DENIED_RETENTION:
            return False
        if event.get("denied") is True:
            return False
        if event.get("provenance_class") != "native":
            return False
        if event.get("source_authority") not in _NATIVE_SOURCE_AUTHORITIES | {"payload_purged"}:
            return False
        if detail["text_hash"] != event.get("text_hash"):
            return False
        if event.get("kind") not in {"message", "edit"} or event.get("direction") not in {"in", "out"}:
            return False
        return True

    def _copy_matches(
        self, copy: Mapping[str, Any], event: Mapping[str, Any], *, require_text: bool
    ) -> bool:
        if str(copy.get("disposition")) == "denied":
            return False
        if str(copy.get("provenance_class")) != "native":
            return False
        if copy.get("source_authority") not in _NATIVE_SOURCE_AUTHORITIES:
            return False
        if any(not isinstance(copy.get(key), str) or not copy.get(key) for key in ("channel", "account", "chat_id", "source_id")):
            return False
        if copy.get("channel") != event.get("channel") or copy.get("account") != event.get("account") or copy.get("chat_id") != event.get("chat_id"):
            return False
        if require_text or isinstance(event.get("text"), str):
            return (
                isinstance(event.get("text"), str)
                and copy.get("text_hash") == event.get("text_hash")
                and copy.get("text_value") == event.get("text")
                and _valid_hash(str(copy.get("text_value") or ""), copy.get("text_hash"))
            )
        return True

    def _event_denied(self, event_id: str, revision: str, event: Mapping[str, Any]) -> bool:
        if event.get("denied") is True:
            return True
        try:
            revision_int = int(revision)
        except ValueError:
            return True
        scope_values = (event.get("channel"), event.get("account"), event.get("chat_id"))
        if any(not isinstance(item, str) or not item for item in scope_values):
            return True
        if HistorySourceAuthority(self.journal).current_source_denied(
            event_id,
            revision_int,
            scope=(scope_values[0], scope_values[1], scope_values[2]),
            native_id=event.get("native_id")
            if isinstance(event.get("native_id"), str)
            else None,
        ):
            return True
        with self.journal.store._lock:
            aliases = self.journal.store._conn.execute(
                "SELECT DISTINCT source_event_id,source_revision FROM history_event_aliases "
                "WHERE canonical_event_id=? AND canonical_revision=?",
                (event_id, revision),
            ).fetchall()
            source_keys = {(event_id, revision_int)}
            for row in aliases:
                try:
                    source_keys.add((str(row["source_event_id"]), int(row["source_revision"])))
                except (TypeError, ValueError):
                    return True
            for source_event_id, source_revision in source_keys:
                if self.journal.store._conn.execute(
                    "SELECT 1 FROM history_source_proofs WHERE event_id=? AND revision=? "
                    "AND revoked_at_ms IS NOT NULL LIMIT 1",
                    (source_event_id, str(source_revision)),
                ).fetchone():
                    return True
            if self.journal.store._conn.execute(
                "SELECT 1 FROM history_event_details WHERE event_id=? AND revision=? AND denied<>0",
                (event_id, revision),
            ).fetchone():
                return True
            if self.journal.store._conn.execute(
                "SELECT 1 FROM history_denials d JOIN history_event_copies c "
                "ON c.source_id=d.source_id AND c.locator_json=d.locator_json "
                "WHERE c.event_id=? AND c.revision=? LIMIT 1",
                (event_id, revision),
            ).fetchone():
                return True
        return False

    def _copies(self, event_id: str, revision: str) -> list[dict[str, Any]]:
        with self.journal.store._lock:
            rows = self.journal.store._conn.execute(
                "SELECT * FROM history_event_copies WHERE event_id=? AND revision=? "
                "ORDER BY copy_id",
                (event_id, revision),
            ).fetchall()
        return [dict(row) for row in rows]

    def _fts_exists(self) -> bool:
        with self.journal.store._lock:
            return self.journal.store._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (_FTS_TABLE,),
            ).fetchone() is not None

    def _remove_indexed_event(self, event_id: str, revision: str) -> None:
        if not self._fts_exists():
            return
        try:
            with self.journal.store._write() as connection:
                connection.execute(
                    f"DELETE FROM {_FTS_TABLE} WHERE event_id=? AND revision=?",
                    (event_id, revision),
                )
        except sqlite3.OperationalError:
            return

    def _scope(
        self,
        context: TrustedReadContext,
        channel: str,
        account: str,
        chat_id: str,
    ) -> tuple[str, str, str] | None:
        if not isinstance(context, TrustedReadContext):
            raise ValidationError("context must be a TrustedReadContext")
        values = (channel, account, chat_id)
        if any(not isinstance(value, str) or not value.strip() for value in values):
            return None
        if context.channel != channel or context.chat_id != chat_id:
            return None
        return channel, account, chat_id

    def _authorized(self, context: TrustedReadContext) -> bool:
        if not isinstance(context, TrustedReadContext):
            raise ValidationError("context must be a TrustedReadContext")
        recipients = context.recipient_principals
        if not recipients or context.principal_id not in recipients:
            return False
        if context.policy_revision != self.policy.current_policy_revision():
            return False
        membership = self.policy.membership(context)
        if membership is None or not membership.members:
            return False
        if context.membership_revision is not None and str(context.membership_revision) != str(membership.revision):
            return False
        return context.principal_id in membership.members and recipients.issubset(membership.members)

    @staticmethod
    def _audience_allows(
        proof: AudienceProof, event: Mapping[str, Any], context: TrustedReadContext
    ) -> bool:
        recipients = context.recipient_principals or frozenset()
        if proof.status == "author_only":
            author = event.get("principal")
            return (
                context.is_direct
                and event.get("chat_kind") in _DIRECT_KINDS
                and isinstance(author, str)
                and recipients == frozenset({author})
                and context.principal_id == author
            )
        return proof.status == "known" and bool(recipients) and recipients.issubset(proof.members)

    @staticmethod
    def _validate_limit(limit: int) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")


def _decode_object(value: Any) -> dict[str, Any] | None:
    try:
        decoded = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _valid_hash(value: str, digest: Any) -> bool:
    if not isinstance(digest, str) or not digest:
        return False
    return hashlib.sha256(value.encode("utf-8")).hexdigest() == digest


def _normalize_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _timestamp(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _timestamp_value(value: Any) -> int | None:
    return int(value) if _timestamp(value) else None


def _time_basis(event: Mapping[str, Any]) -> str:
    if _timestamp(event.get("occurred_ms")) and event.get("time_certainty") in {
        "canonical", "certain", "exact", "source_exact", "verified", "native", "provider_timestamp"
    }:
        return "occurred"
    if _timestamp(event.get("observed_ms")):
        return "capture"
    return "unknown"


def _time_certainty(event: Mapping[str, Any]) -> str:
    basis = _time_basis(event)
    if basis == "occurred":
        return str(event.get("time_certainty"))
    if basis == "capture":
        return "approximate_capture"
    return "unknown"


def _query_time(event: Mapping[str, Any]) -> int | None:
    occurred = _timestamp_value(event.get("occurred_ms"))
    if occurred is not None and _time_basis(event) == "occurred":
        return occurred
    if _time_basis(event) == "capture":
        return _timestamp_value(event.get("observed_ms"))
    return None


def _sort_key(event: Mapping[str, Any]) -> tuple[int, str, int]:
    return (
        _query_time(event) or 0,
        str(event.get("event_id") or ""),
        int(event.get("revision") or 0),
    )


def _revision_value(value: Any) -> int | str | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return value if isinstance(value, str) else None


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _safe_reference(value: Any) -> Any:
    if isinstance(value, str):
        return value
    if not isinstance(value, Mapping):
        return None
    safe = {
        key: value[key]
        for key in ("event_id", "revision", "native_id", "source_id")
        if isinstance(value.get(key), (str, int)) and not isinstance(value.get(key), bool)
    }
    return safe or None


def _empty_context(result: KnowledgeContext, reason: str) -> KnowledgeContext:
    return KnowledgeContext(
        text="",
        statement_ids=(),
        source_refs=(),
        identity_revision=result.identity_revision,
        acl_epoch=result.acl_epoch,
        reason=reason,
    )
