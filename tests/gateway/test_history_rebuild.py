from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from yeoman_gateway.knowledge._history import HistoricalJournal, HistorySourceAuthority
from yeoman_gateway.knowledge._history_adapters import read_catalogued_events
from yeoman_gateway.knowledge._history_reader import HistoryReader
from yeoman_gateway.knowledge._history_rebuild import (
    rebuild_history,
    verify_history,
)
from yeoman_gateway.knowledge.authority import EvidenceAudience
from yeoman_gateway.knowledge.models import SourceRef

_WHEN = 1_725_000_000_000


def _event(
    event_id: str,
    native_id: str,
    text: str | None,
    *,
    channel: str = "whatsapp",
    account: str | None = "acct-a",
    chat_id: str = "chat-a@g.us",
    kind: str = "MESSAGE",
    direction: str = "in",
    revision: int = 1,
    target: str | None = None,
    purged_ms: int | None = None,
) -> dict[str, Any]:
    payload = None if text is None else json.dumps({"text": text})
    return {
        "event_id": event_id,
        "revision": revision,
        "kind": kind,
        "direction": direction,
        "origin": "whatsapp_canonical",
        "channel": channel,
        "account": account,
        "chat_id": chat_id,
        "principal": "whatsapp:alice",
        "source_message_id": native_id,
        "target_message_id": target,
        "occurred_ms": _WHEN,
        "created_ms": _WHEN,
        "payload_json": payload,
        "payload_purged_ms": purged_ms,
    }


def _inbound(native_id: str, text: str, *, channel: str = "whatsapp", account: str | None = "acct-a", chat_id: str = "chat-a@g.us", sender_name: str = "") -> dict[str, Any]:
    return {
        "message_id": native_id,
        "timestamp": _WHEN,
        "created_at": "2026-09-01T10:00:00+00:00",
        "text": text,
        "type": "text",
        "channel": channel,
        "account": account,
        "chat_id": chat_id,
        "sender_id": "whatsapp:alice",
        "sender_name": sender_name,
        "reply_to_message_id": None,
    }


def _authority(event_id: str, *, revision: int = 1, account_is_known: bool = True, revoked_at_ms: int | None = None, members: tuple[str, ...] = ("whatsapp:alice", "whatsapp:bob")) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "revision": revision,
        "author_principal": "whatsapp:alice",
        "source_channel": "whatsapp",
        "source_chat_id": "chat-a@g.us",
        "occurred_at_ms": _WHEN,
        "audience_status": "known",
        "audience_members_json": json.dumps(list(members)),
        "audience_snapshot_id": "snapshot-1",
        "policy_revision": "17",
        "revoked_at_ms": revoked_at_ms,
        "revoking_event_id": "evt-revoke" if revoked_at_ms is not None else None,
        "created_ms": _WHEN,
        "updated_ms": _WHEN,
        "_account_is_known": account_is_known,
    }


def _write_source(root: Path, source_id: str, *, events=(), inbound=(), authorities=(), nodes=(), files=None) -> dict[str, Any]:
    source_root = root / "sources" / source_id
    source_root.mkdir(parents=True)
    database = source_root / "processing.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE events (
          event_id TEXT, revision INTEGER, kind TEXT, direction TEXT, origin TEXT,
          channel TEXT, account TEXT, chat_id TEXT, principal TEXT,
          source_message_id TEXT, target_message_id TEXT, occurred_ms INTEGER,
          created_ms INTEGER, payload_json TEXT, payload_purged_ms INTEGER
        );
        CREATE TABLE inbound_messages (
          message_id TEXT, timestamp INTEGER, created_at TEXT, text TEXT, type TEXT,
          channel TEXT, account TEXT, chat_id TEXT, sender_id TEXT,
          sender_name TEXT, reply_to_message_id TEXT
        );
        CREATE TABLE memory2_nodes (
          id TEXT, source_message_id TEXT, kind TEXT, is_deleted INTEGER,
          channel TEXT, account TEXT, chat_id TEXT, sender_id TEXT, source_role TEXT,
          created_at TEXT, content TEXT
        );
        CREATE TABLE effects (effect_id TEXT, payload_json TEXT, target_json TEXT, state TEXT, created_ms INTEGER, payload_kind TEXT, principal TEXT);
        CREATE TABLE event_source_authority (
          event_id TEXT, revision INTEGER, author_principal TEXT, source_channel TEXT,
          source_chat_id TEXT, occurred_at_ms INTEGER, audience_status TEXT,
          audience_members_json TEXT, audience_snapshot_id TEXT, policy_revision TEXT,
          revoked_at_ms INTEGER, revoking_event_id TEXT, created_ms INTEGER, updated_ms INTEGER,
          PRIMARY KEY (event_id, revision)
        );
        """
    )
    event_columns = ("event_id", "revision", "kind", "direction", "origin", "channel", "account", "chat_id", "principal", "source_message_id", "target_message_id", "occurred_ms", "created_ms", "payload_json", "payload_purged_ms")
    inbound_columns = ("message_id", "timestamp", "created_at", "text", "type", "channel", "account", "chat_id", "sender_id", "sender_name", "reply_to_message_id")
    node_columns = ("id", "source_message_id", "kind", "is_deleted", "channel", "account", "chat_id", "sender_id", "source_role", "created_at", "content")
    authority_columns = ("event_id", "revision", "author_principal", "source_channel", "source_chat_id", "occurred_at_ms", "audience_status", "audience_members_json", "audience_snapshot_id", "policy_revision", "revoked_at_ms", "revoking_event_id", "created_ms", "updated_ms")
    connection.executemany(f"INSERT INTO events VALUES ({','.join('?' for _ in event_columns)})", [tuple(row.get(key) for key in event_columns) for row in events])
    connection.executemany(f"INSERT INTO inbound_messages VALUES ({','.join('?' for _ in inbound_columns)})", [tuple(row.get(key) for key in inbound_columns) for row in inbound])
    connection.executemany(f"INSERT INTO memory2_nodes VALUES ({','.join('?' for _ in node_columns)})", [tuple(row.get(key) for key in node_columns) for row in nodes])
    connection.executemany(f"INSERT INTO event_source_authority VALUES ({','.join('?' for _ in authority_columns)})", [tuple(row.get(key) for key in authority_columns) for row in authorities])
    connection.commit()
    connection.close()

    copied = {"processing.db": {"copied_sha256": hashlib.sha256(database.read_bytes()).hexdigest()}}
    for name, content in (files or {}).items():
        path = source_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        copied[name] = {"copied_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    return {"source_id": source_id, "status": "copied", "copied_files": copied}


def _collection(root: Path, *sources: dict[str, Any]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "sources").mkdir(exist_ok=True)
    (root / "manifest.json").write_text(
        json.dumps({"source_bundle_manifest_version": 2, "complete": True, "sources": list(sources)}),
        encoding="utf-8",
    )
    return root


def _rows(target: Path, query: str, params=()) -> list[sqlite3.Row]:
    connection = sqlite3.connect(target / "data" / "processing.db")
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(query, params).fetchall()
    finally:
        connection.close()


def test_all_live_statement_source_ids_and_revisions_resolve(tmp_path: Path) -> None:
    collection = _collection(
        tmp_path / "collection",
        _write_source(tmp_path / "collection", "canonical", events=[_event("evt-live", "native-1", "hello")], authorities=[_authority("evt-live")]),
    )
    knowledge = tmp_path / "knowledge.db"
    connection = sqlite3.connect(knowledge)
    connection.execute("CREATE TABLE knowledge_statement_sources (statement_id TEXT,event_id TEXT,revision INTEGER)")
    connection.execute("INSERT INTO knowledge_statement_sources VALUES ('s1','evt-live',1)")
    connection.commit()
    connection.close()

    target = tmp_path / "rebuilt"
    report = rebuild_history(collection=collection, target_home=target)
    verified = verify_history(target_home=target, knowledge_snapshot=knowledge)
    with HistoricalJournal(target, create=False) as journal:
        source = HistorySourceAuthority(journal).verify_source_ref("evt-live", 1)
        assert source is not None
        assert source.occurred_at_ms == _WHEN
        assert HistorySourceAuthority(journal).evidence_audience(source, basis="history") is not None
    assert report["complete"]
    assert verified["resolved_source_count"] == 1
    assert verified["missing_source_refs"] == []


def test_identical_message_copies_keep_provenance_and_live_ids(tmp_path: Path) -> None:
    collection = _collection(
        tmp_path / "collection",
        _write_source(tmp_path / "collection", "canonical", events=[_event("evt-copy", "native-copy", "same")]),
        _write_source(tmp_path / "collection", "reply_context", inbound=[_inbound("native-copy", "same", sender_name="Alice")]),
    )
    target = tmp_path / "rebuilt"

    rebuild_history(collection=collection, target_home=target)

    assert [row["event_id"] for row in _rows(target, "SELECT event_id FROM events")] == ["evt-copy"]
    copies = _rows(target, "SELECT source_id,source_kind,text_value FROM history_event_copies ORDER BY source_id")
    assert {(row["source_id"], row["source_kind"], row["text_value"]) for row in copies} == {
        ("canonical", "journal", "same"),
        ("reply_context", "inbound_messages", "same"),
    }
    names = _rows(target, "SELECT source_id,locator_json,name,raw_identifier,occurred_ms,time_certainty FROM history_name_observations")
    assert [(row["source_id"], row["name"], row["raw_identifier"]) for row in names] == [
        ("reply_context", "Alice", "whatsapp:alice")
    ]
    assert json.loads(names[0]["locator_json"])["table"] == "inbound_messages"
    assert names[0]["occurred_ms"] == _WHEN
    assert names[0]["time_certainty"] == "provider_timestamp"


def test_conflicting_text_and_edit_revisions_are_retained(tmp_path: Path) -> None:
    rows = [
        _event("evt-one", "native-conflict", "one"),
        _event("evt-two", "native-conflict", "two"),
        _event("evt-edit", "native-conflict", "edited", kind="MESSAGE_EDIT", revision=2, target="native-conflict"),
    ]
    collection = _collection(tmp_path / "collection", _write_source(tmp_path / "collection", "canonical", events=rows))
    target = tmp_path / "rebuilt"

    report = rebuild_history(collection=collection, target_home=target)

    assert report["conflict_count"] == 0
    assert {row["event_id"] for row in _rows(target, "SELECT event_id FROM events")} == {"evt-one", "evt-two", "evt-edit"}
    assert {row["text_value"] for row in _rows(target, "SELECT text_value FROM history_event_copies") if row["text_value"]} == {"one", "two", "edited"}
    assert "2" in {row["revision"] for row in _rows(target, "SELECT revision FROM history_event_details")}


def test_current_retention_and_revocation_follow_only_exact_compatible_aliases(
    tmp_path: Path,
) -> None:
    root = tmp_path / "synthetic-collection"
    sources = [
        _write_source(root, name, events=[_event("shared-source-id", native_id, text)])
        for name, native_id, text in (
            ("a", "shared-native-id", "original"),
            ("b", "shared-native-id", "variant one"),
            ("c", "shared-native-id", "variant two"),
            ("d", "other-native-id", "other identity"),
        )
    ]
    target = tmp_path / "synthetic-rebuilt"

    report = rebuild_history(collection=_collection(root, *sources), target_home=target)

    assert report["conflict_count"] == 3
    with HistoricalJournal(target, create=False) as journal:
        rows = _rows(
            target,
            "SELECT DISTINCT a.canonical_event_id,a.canonical_revision,c.native_id "
            "FROM history_event_aliases a JOIN history_event_copies c "
            "ON c.event_id=a.canonical_event_id AND c.revision=a.canonical_revision "
            "AND c.source_id=a.source_id AND c.locator_json=a.locator_json "
            "WHERE a.source_event_id=? AND a.canonical_event_id<>?",
            ("shared-source-id", "shared-source-id"),
        )
        shared = [row for row in rows if row["native_id"] == "shared-native-id"]
        other = [row for row in rows if row["native_id"] == "other-native-id"]
        assert len(shared) == 2
        assert len(other) == 1
        first, sibling = shared
        authority = HistorySourceAuthority(journal)
        with journal.store._write() as connection:
            connection.execute(
                "UPDATE events SET payload_purged_ms=? WHERE event_id=? AND revision=?",
                (_WHEN + 1, other[0]["canonical_event_id"], other[0]["canonical_revision"]),
            )
        scope = ("whatsapp", "acct-a", "chat-a@g.us")
        assert not authority.current_retention_denied(
            first["canonical_event_id"], int(first["canonical_revision"]), scope=scope
        ), "A different native identity must not taint this source"

        with journal.store._write() as connection:
            connection.execute(
                "UPDATE events SET payload_purged_ms=? WHERE event_id=? AND revision=?",
                (_WHEN + 2, sibling["canonical_event_id"], sibling["canonical_revision"]),
            )
        assert authority.current_retention_denied(
            first["canonical_event_id"], int(first["canonical_revision"]), scope=scope
        ), "Current marker did not reach the exact-compatible sibling"

        with journal.store._write() as connection:
            connection.execute(
                "UPDATE events SET payload_purged_ms=NULL WHERE event_id=? AND revision=?",
                (sibling["canonical_event_id"], sibling["canonical_revision"]),
            )
        first_source = SourceRef(
            str(first["canonical_event_id"]), int(first["canonical_revision"]),
            "whatsapp", "chat-a@g.us", "whatsapp:alice", _WHEN,
        )
        audience = EvidenceAudience.known(frozenset({"whatsapp:alice", "whatsapp:bob"}))
        reader = HistoryReader.__new__(HistoryReader)
        reader.journal = journal
        with journal.store._lock:
            detail = journal.store._conn.execute(
                "SELECT normalized_json FROM history_event_details WHERE event_id=? AND revision=?",
                (first_source.event_id, str(first_source.revision)),
            ).fetchone()
        assert detail is not None
        event = json.loads(detail["normalized_json"])
        other_source = SourceRef(
            str(other[0]["canonical_event_id"]), int(other[0]["canonical_revision"]),
            "whatsapp", "chat-a@g.us", "whatsapp:alice", _WHEN,
        )
        journal.store.upsert_event_source_authority(
            source=other_source, audience=audience, now_ms=_WHEN + 3
        )
        with journal.store._write() as connection:
            connection.execute(
                "UPDATE event_source_authority SET revoked_at_ms=?,revoking_event_id=? "
                "WHERE event_id=? AND revision=?",
                (
                    _WHEN + 4,
                    "evt-revoke",
                    other_source.event_id,
                    other_source.revision,
                ),
            )
        assert not reader._event_denied(
            first_source.event_id, str(first_source.revision), event
        ), "Current source revocation crossed an unrelated native identity"

        sibling_source = SourceRef(
            str(sibling["canonical_event_id"]), int(sibling["canonical_revision"]),
            "whatsapp", "chat-a@g.us", "whatsapp:alice", _WHEN,
        )
        journal.store.upsert_event_source_authority(
            source=sibling_source, audience=audience, now_ms=_WHEN + 5
        )
        with journal.store._write() as connection:
            updated = connection.execute(
                "UPDATE event_source_authority SET revoked_at_ms=?,revoking_event_id=? "
                "WHERE event_id=? AND revision=?",
                (
                    _WHEN + 6,
                    "evt-revoke",
                    sibling_source.event_id,
                    sibling_source.revision,
                ),
            )
            assert updated.rowcount == 1
        assert reader._event_denied(
            first_source.event_id, str(first_source.revision), event
        ), "Current source revocation on an exact-compatible sibling did not deny reader output"
        assert authority.source_revoked(
            first_source
        ), "Current source revocation did not reach the exact-compatible sibling"


def test_delete_before_message_and_unresolved_delete_are_reported(tmp_path: Path) -> None:
    rows = [
        _event("a-delete", "delete-native", None, kind="MESSAGE_DELETE", target="victim"),
        _event("m-message", "victim", "must not return"),
        _event("z-orphan-delete", "orphan-delete", None, kind="MESSAGE_DELETE", target="absent"),
    ]
    collection = _collection(tmp_path / "collection", _write_source(tmp_path / "collection", "canonical", events=rows))
    target = tmp_path / "rebuilt"

    report = rebuild_history(collection=collection, target_home=target)

    victim = _rows(target, "SELECT text_value,disposition FROM history_event_copies WHERE native_id='victim'")
    assert victim and all(row["text_value"] is None and row["disposition"] == "denied" for row in victim)
    assert report["unresolved_count"] == 1
    assert _rows(target, "SELECT 1 FROM history_unresolved_refs WHERE reason='delete_target_unresolved'")


def test_whatsapp_accidental_purge_recovers_but_other_retention_does_not(tmp_path: Path) -> None:
    canonical = _write_source(
        tmp_path / "collection", "canonical",
        events=[_event("wa-purged", "wa-native", None, purged_ms=_WHEN), _event("tg-purged", "tg-native", None, channel="telegram", purged_ms=_WHEN)],
    )
    other = _write_source(
        tmp_path / "collection", "reply_context",
        inbound=[_inbound("wa-native", "restore wa"), _inbound("tg-native", "do not restore", channel="telegram")],
    )
    collection = _collection(tmp_path / "collection", canonical, other)
    target = tmp_path / "rebuilt"

    rebuild_history(collection=collection, target_home=target)

    wa = json.loads(_rows(target, "SELECT payload_json FROM events WHERE event_id='wa-purged'")[0][0])
    tg = json.loads(_rows(target, "SELECT payload_json FROM events WHERE event_id='tg-purged'")[0][0])
    assert wa["text"] == "restore wa"
    assert tg["text"] is None
    copies = _rows(target, "SELECT source_id,text_value FROM history_event_copies WHERE native_id='wa-native'")
    assert {row["source_id"]: row["text_value"] for row in copies} == {
        "canonical": None,
        "reply_context": "restore wa",
    }


def test_revoked_or_suppressed_copy_cannot_resurrect_from_another_source(tmp_path: Path) -> None:
    canonical = _write_source(
        tmp_path / "collection", "canonical",
        events=[
            _event("evt-revoked", "native-revoked", "secret"),
            _event("evt-suppressed", "native-suppressed", "hidden"),
            _event("evt-erased", "native-erased", "erase me"),
        ],
        authorities=[_authority("evt-revoked", revoked_at_ms=_WHEN)],
    )
    erased_node = {
        "id": "deleted-1", "source_message_id": "native-erased", "kind": "summary",
        "is_deleted": 1, "channel": "whatsapp", "account": "acct-a",
        "chat_id": "chat-a@g.us", "sender_id": "whatsapp:alice", "source_role": "user",
        "created_at": "2026-09-01T10:00:00+00:00", "content": "erase me",
    }
    raw = _write_source(
        tmp_path / "collection", "raw_archive",
        inbound=[
            _inbound("native-revoked", "different secret copy"),
            _inbound("native-suppressed", "hidden"),
            _inbound("native-erased", "erase me"),
        ],
        nodes=[erased_node],
        files={"SUPPRESSIONS": json.dumps({"channel": "whatsapp", "chat_id": "chat-a@g.us", "native_id": "native-suppressed", "reason": "owner"}) + "\n"},
    )
    collection = _collection(tmp_path / "collection", canonical, raw)
    target = tmp_path / "rebuilt"

    rebuild_history(collection=collection, target_home=target)

    for native_id in ("native-revoked", "native-suppressed", "native-erased"):
        rows = _rows(target, "SELECT text_value,disposition FROM history_event_copies WHERE native_id=?", (native_id,))
        assert rows and all(row["text_value"] is None and row["disposition"] == "denied" for row in rows)


def test_raw_archive_suppression_uses_archive_event_id(tmp_path: Path) -> None:
    raw_event = {
        "channel": "whatsapp", "account": "acct-a", "chat_id": "chat-a@g.us",
        "direction": "in", "kind": "MESSAGE",
        "native": {
            "eventId": "archive-frame-1", "type": "message",
            "payload": {
                "remoteJid": "chat-a@g.us", "messageId": "transport-message-1",
                "messageTimestamp": 1725000000, "senderId": "whatsapp:alice", "text": "hidden",
            },
        },
    }
    raw = _write_source(
        tmp_path / "collection", "raw_archive",
        files={
            "data/raw/whatsapp/2026-10.jsonl": json.dumps(raw_event) + "\n",
            "data/raw/SUPPRESSIONS": json.dumps({
                "channel": "whatsapp", "chat_id": "chat-a@g.us",
                "native_id": "archive-frame-1", "reason": "owner",
            }) + "\n",
        },
    )
    canonical = _write_source(
        tmp_path / "collection", "canonical",
        events=[_event("evt-transport", "transport-message-1", "hidden")],
    )
    collection = _collection(tmp_path / "collection", canonical, raw)
    target = tmp_path / "rebuilt"

    rebuild_history(collection=collection, target_home=target)

    copies = _rows(target, "SELECT text_value,disposition FROM history_event_copies WHERE native_id='transport-message-1'")
    assert copies and all(row["text_value"] is None and row["disposition"] == "denied" for row in copies)


def test_source_ids_and_revisions_keep_authority_closure(tmp_path: Path) -> None:
    root = tmp_path / "collection"
    sources = [
        _write_source(root, "canonical_a", events=[_event("live-a", "native-dupe", "same")], authorities=[_authority("live-a")]),
        _write_source(root, "canonical_b", events=[_event("live-b", "native-dupe", "same")], authorities=[_authority("live-b")]),
        _write_source(
            root, "canonical_revision",
            events=[
                _event("live-revision", "native-revision", "before", revision=1),
                _event("live-revision", "native-revision", "after", kind="MESSAGE_EDIT", revision=2, target="native-revision"),
            ],
            authorities=[_authority("live-revision", revision=1), _authority("live-revision", revision=2)],
        ),
    ]
    collection = _collection(root, *sources)
    knowledge = tmp_path / "knowledge.db"
    connection = sqlite3.connect(knowledge)
    connection.execute("CREATE TABLE knowledge_statement_sources (statement_id TEXT,event_id TEXT,revision INTEGER)")
    connection.executemany(
        "INSERT INTO knowledge_statement_sources VALUES (?,?,?)",
        [("s1", "live-a", 1), ("s2", "live-b", 1), ("s3", "live-revision", 1), ("s4", "live-revision", 2)],
    )
    connection.commit()
    connection.close()
    target = tmp_path / "rebuilt"

    rebuild_history(collection=collection, target_home=target)
    verified = verify_history(target_home=target, knowledge_snapshot=knowledge)

    with HistoricalJournal(target, create=False) as journal:
        authority = HistorySourceAuthority(journal)
        assert all(authority.verify_source_ref(event_id, revision) is not None for event_id, revision in [
            ("live-a", 1), ("live-b", 1), ("live-revision", 1), ("live-revision", 2),
        ])
    assert verified["resolved_source_count"] == 4
    assert verified["missing_source_refs"] == []


def test_verified_wal_sidecar_keeps_revocation_denial(tmp_path: Path) -> None:
    root = tmp_path / "collection"
    source = _write_source(root, "canonical", events=[_event("evt-wal", "native-wal", "secret")])
    database = root / "sources" / "canonical" / "processing.db"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA journal_mode=WAL")
    authority = _authority("evt-wal", revoked_at_ms=_WHEN)
    columns = (
        "event_id", "revision", "author_principal", "source_channel", "source_chat_id",
        "occurred_at_ms", "audience_status", "audience_members_json", "audience_snapshot_id",
        "policy_revision", "revoked_at_ms", "revoking_event_id", "created_ms", "updated_ms",
    )
    connection.execute(
        f"INSERT INTO event_source_authority VALUES ({','.join('?' for _ in columns)})",
        tuple(authority.get(key) for key in columns),
    )
    connection.commit()
    source["copied_files"]["processing.db"] = {
        "copied_sha256": hashlib.sha256(database.read_bytes()).hexdigest()
    }
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(f"{database}{suffix}")
        if sidecar.is_file():
            relative = f"processing.db{suffix}"
            source["copied_files"][relative] = {"copied_sha256": hashlib.sha256(sidecar.read_bytes()).hexdigest()}
    collection = _collection(root, source)
    target = tmp_path / "rebuilt"
    try:
        rebuild_history(collection=collection, target_home=target)
    finally:
        connection.close()

    copies = _rows(target, "SELECT text_value,disposition FROM history_event_copies WHERE native_id='native-wal'")
    assert copies and all(row["text_value"] is None and row["disposition"] == "denied" for row in copies)


def test_revoked_source_id_collision_does_not_deny_other_account(tmp_path: Path) -> None:
    root = tmp_path / "collection"
    source_a = _write_source(
        root, "source_a",
        events=[_event("same-live-id", "native", "a", account="acct-a")],
        authorities=[_authority("same-live-id", revoked_at_ms=_WHEN)],
    )
    source_b = _write_source(
        root, "source_b",
        events=[_event("same-live-id", "native", "b", account="acct-b")],
    )
    collection = _collection(root, source_a, source_b)
    target = tmp_path / "rebuilt"

    rebuild_history(collection=collection, target_home=target)

    copies = _rows(target, "SELECT account,text_value,disposition FROM history_event_copies ORDER BY account")
    assert [(row["account"], row["text_value"], row["disposition"]) for row in copies] == [
        ("acct-a", None, "denied"),
        ("acct-b", "b", "conflict_variant"),
    ]


def test_authority_is_bound_to_its_verified_database_file(tmp_path: Path) -> None:
    root = tmp_path / "collection"
    source = _write_source(
        root, "snapshot",
        events=[_event("same-id", "native", "a", account="acct-a")],
        authorities=[_authority("same-id", revoked_at_ms=_WHEN)],
    )
    _write_source(
        tmp_path / "alternate-fixture", "other",
        events=[_event("same-id", "native", "b", account="acct-b")],
    )
    alternate_database = root / "sources" / "snapshot" / "alternate.db"
    alternate_database.write_bytes(
        (tmp_path / "alternate-fixture" / "sources" / "other" / "processing.db").read_bytes()
    )
    source["copied_files"]["alternate.db"] = {
        "copied_sha256": hashlib.sha256(alternate_database.read_bytes()).hexdigest()
    }
    target = tmp_path / "rebuilt"

    rebuild_history(collection=_collection(root, source), target_home=target)

    copies = _rows(target, "SELECT account,text_value,disposition FROM history_event_copies ORDER BY account")
    assert [(row["account"], row["text_value"]) for row in copies] == [
        ("acct-a", None),
        ("acct-b", "b"),
    ]
    assert all(row["disposition"] != "denied" for row in copies if row["account"] == "acct-b")


def test_original_source_ref_with_ambiguous_canonical_targets_is_unusable(tmp_path: Path) -> None:
    root = tmp_path / "collection"
    sources = [
        _write_source(
            root, "source_a",
            events=[_event("same-id", "native", "a", account="acct-a")],
            authorities=[_authority("same-id")],
        ),
        _write_source(
            root, "source_b",
            events=[_event("same-id", "native", "b", account="acct-b")],
        ),
    ]
    target = tmp_path / "rebuilt"

    rebuild_history(collection=_collection(root, *sources), target_home=target)

    with HistoricalJournal(target, create=False) as journal:
        assert HistorySourceAuthority(journal).verify_source_ref("same-id", 1) is None


def test_unknown_account_suppression_denies_its_source_copy(tmp_path: Path) -> None:
    raw_event = {
        "channel": "whatsapp", "account": None, "chat_id": "chat-a@g.us",
        "native_id": "archive-event", "kind": "message", "direction": "in",
        "native": {
            "eventId": "archive-event", "type": "MESSAGE",
            "payload": {
                "messageId": "transport-message", "chatJid": "chat-a@g.us",
                "text": "suppressed content", "timestamp": _WHEN,
            },
        },
    }
    source = _write_source(
        tmp_path / "collection", "raw",
        files={
            "raw.jsonl": json.dumps(raw_event) + "\n",
            "SUPPRESSIONS": json.dumps({
                "channel": "whatsapp", "chat_id": "chat-a@g.us",
                "native_id": "archive-event", "reason": "owner",
            }) + "\n",
        },
    )
    collection = _collection(tmp_path / "collection", source)
    target = tmp_path / "rebuilt"

    report = rebuild_history(collection=collection, target_home=target)

    copies = _rows(target, "SELECT source_id,account,text_value,disposition FROM history_event_copies")
    assert [(row["source_id"], row["account"], row["text_value"], row["disposition"]) for row in copies] == [
        ("raw", None, None, "denied"),
    ]
    payload = json.loads(_rows(target, "SELECT payload_json FROM events")[0][0])
    assert payload["text"] is None
    assert report["unresolved_count"] == 1


def test_unknown_source_time_still_applies_exact_local_revocation(tmp_path: Path) -> None:
    event = _event("revoked-unknown-time", "native", "revoked text")
    event["occurred_ms"] = None
    event["created_ms"] = None
    authority = _authority("revoked-unknown-time", revoked_at_ms=_WHEN)
    authority["occurred_at_ms"] = 0
    collection = _collection(
        tmp_path / "collection",
        _write_source(
            tmp_path / "collection", "native", events=[event], authorities=[authority]
        ),
    )
    target = tmp_path / "rebuilt"

    report = rebuild_history(collection=collection, target_home=target)

    copies = _rows(target, "SELECT text_value,disposition FROM history_event_copies")
    assert [(row["text_value"], row["disposition"]) for row in copies] == [(None, "denied")]
    assert report["unresolved_count"] == 1
    with HistoricalJournal(target, create=False) as journal:
        assert HistorySourceAuthority(journal).verify_source_ref("revoked-unknown-time", 1) is None


def test_curated_denial_binds_unique_legacy_source_id(tmp_path: Path) -> None:
    root = tmp_path / "collection"
    node = {
        "id": "existing-node", "source_message_id": "native", "kind": "utterance",
        "is_deleted": 0, "channel": "whatsapp", "account": "acct-a",
        "chat_id": "chat-a@g.us", "sender_id": "whatsapp:alice", "source_role": "user",
        "created_at": "2026-09-01T10:00:00+00:00", "content": "curated revoked text",
    }
    curated = _write_source(root, "knowledge", nodes=[node])
    database = root / "sources" / "knowledge" / "processing.db"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE knowledge_statement_sources "
        "(statement_id TEXT,event_id TEXT,revision INTEGER,status TEXT)"
    )
    connection.execute(
        "INSERT INTO knowledge_statement_sources VALUES (?,?,?,?)",
        ("s", "legacy-node:existing-node", 1, "revoked"),
    )
    connection.commit()
    connection.close()
    curated["copied_files"]["processing.db"] = {
        "copied_sha256": hashlib.sha256(database.read_bytes()).hexdigest()
    }
    collection = _collection(root, curated)
    target = tmp_path / "rebuilt"

    report = rebuild_history(collection=collection, target_home=target)

    copies = _rows(target, "SELECT text_value,disposition FROM history_event_copies")
    assert [(row["text_value"], row["disposition"]) for row in copies] == [(None, "denied")]
    assert report["unresolved_count"] == 0
    with HistoricalJournal(target, create=False) as journal:
        assert HistorySourceAuthority(journal).verify_source_ref("legacy-node:existing-node", 1) is None


def test_native_bridge_reference_tree_dispatches_by_verified_manifest_metadata(
    tmp_path: Path,
) -> None:
    root = tmp_path / "collection"
    entry = _write_source(
        root,
        "src-98799c741f2a1eb5",
        files={
            "native-message.json": json.dumps({
                "encoded": "bm90LWEtYnJpZGdlLXByb3Rv",
                "chatJid": "chat-a@g.us",
                "messageId": "native-1",
            }),
            "unrelated.json": json.dumps({
                "message_id": "should-not-become-native",
                "timestamp": 1_725_000_000,
                "text": "ordinary metadata",
            }),
        },
    )
    entry.update(
        kind="tree",
        source_class="native",
        source_path=str(tmp_path / "bridge" / "whatsapp-message-references"),
    )
    collection = _collection(root, entry)

    events, report = read_catalogued_events(collection=collection)

    assert len(events) == 1
    assert events[0].source_kind == "bridge_reference"
    assert events[0].native_id == "native-1"
    assert events[0].provenance_class == "unknown"
    assert events[0].account is None
    assert report["parsed_count"] == 1
    assert report["unresolved_count"] == 1
    assert report["omitted_counts"]["bridge_reference_encoded_missing"] == 1


def test_derived_only_cannot_certify_authority(tmp_path: Path) -> None:
    node = {
        "id": "legacy-1", "source_message_id": "native-legacy", "kind": "summary",
        "is_deleted": 0, "channel": "whatsapp", "account": "acct-a",
        "chat_id": "chat-a@g.us", "sender_id": "whatsapp:alice", "source_role": "user",
        "created_at": "2026-09-01T10:00:00+00:00", "content": "derived summary",
    }
    collection = _collection(
        tmp_path / "collection",
        _write_source(tmp_path / "collection", "legacy", nodes=[node], authorities=[_authority("legacy-node:legacy-1")]),
    )
    target = tmp_path / "rebuilt"

    rebuild_history(collection=collection, target_home=target)

    with HistoricalJournal(target, create=False) as journal:
        assert HistorySourceAuthority(journal).verify_source_ref("legacy-node:legacy-1", 1) is None


def test_unknown_identity_or_account_is_not_guessed(tmp_path: Path) -> None:
    row = _event("evt-unknown-account", "native-unknown-account", "unknown", account=None, kind="PARTICIPANT_ADD", direction="unknown")
    collection = _collection(
        tmp_path / "collection",
        _write_source(tmp_path / "collection", "canonical", events=[row], authorities=[_authority("evt-unknown-account")]),
    )
    target = tmp_path / "rebuilt"

    rebuild_history(collection=collection, target_home=target)

    details = json.loads(_rows(target, "SELECT normalized_json FROM history_event_details")[0][0])
    assert details["account"] is None
    assert details["kind"] == "membership"
    assert details["direction"] == "unknown"
    with HistoricalJournal(target, create=False) as journal:
        assert HistorySourceAuthority(journal).verify_source_ref("evt-unknown-account", 1) is None


def test_retention_and_delete_do_not_cross_accounts(tmp_path: Path) -> None:
    collection = _collection(
        tmp_path / "collection",
        _write_source(
            tmp_path / "collection",
            "canonical-a",
            events=[
                _event("a-delete", "delete-a", None, kind="MESSAGE_DELETE", target="shared-native", account="acct-a"),
                _event("shared-message", "shared-native", None, account="acct-a", purged_ms=_WHEN),
            ],
        ),
        _write_source(
            tmp_path / "collection",
            "canonical-b",
            events=[_event("shared-message", "shared-native", "account b", account="acct-b")],
        ),
    )
    target = tmp_path / "rebuilt"

    rebuild_history(collection=collection, target_home=target)

    a = _rows(target, "SELECT text_value FROM history_event_copies WHERE account='acct-a'")
    b = _rows(target, "SELECT text_value FROM history_event_copies WHERE account='acct-b'")
    assert a and all(row["text_value"] is None for row in a)
    assert b and all(row["text_value"] == "account b" for row in b)


def test_rebuild_target_isolated_atomic_and_failure_not_complete(tmp_path: Path) -> None:
    root = tmp_path / "collection"
    source = _write_source(root, "canonical", events=[_event("evt", "native", "text")])
    collection = _collection(root, source)
    source_path = root / "sources" / "canonical" / "processing.db"
    source_path.write_bytes(source_path.read_bytes() + b"changed")
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(ValueError):
        rebuild_history(collection=collection, target_home=target)

    assert target.is_dir() and list(target.iterdir()) == []
