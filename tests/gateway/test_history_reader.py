"""Protected reads over an explicitly rebuilt history journal."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from yeoman_gateway.knowledge._history import HistoricalJournal, HistorySourceAuthority
from yeoman_gateway.knowledge._history_audience import HistoryAudience, roster_confirmation
from yeoman_gateway.knowledge.api import open_knowledge_store, workspace_id_for
from yeoman_gateway.knowledge.authority import EvidenceAudience
from yeoman_gateway.knowledge.models import (
    KnowledgeContext,
    RecallQuery,
    SourceRef,
    StatementCandidate,
    TrustedAdminContext,
    TrustedCaptureContext,
    TrustedReadContext,
)
from yeoman_gateway.knowledge.runtime import RuntimeKnowledgePolicy

NOW = 1_736_899_200_000  # 2025-01-15 UTC
GROUP = "history-group@g.us"
DIRECT = "reader@s.whatsapp.net"
READER = "whatsapp:reader@s.whatsapp.net"


class _Registry:
    def __init__(self, members: set[str] | None = None, *, chat_id: str = GROUP) -> None:
        self.members = members or {READER}
        self.chat_id = chat_id

    def get_chat(self, channel: str, chat_id: str) -> dict[str, Any]:
        assert (channel, chat_id) == ("whatsapp", self.chat_id)
        return {
            "metadata": {"participants": sorted(self.members)},
            "last_sync_at": "roster-r1",
        }


def _context(
    *,
    recipient: str = READER,
    additional_recipients: tuple[str, ...] = (),
    policy_revision: int = 7,
    membership_revision: str | None = "roster-r1",
    chat_id: str = GROUP,
    is_direct: bool = False,
) -> TrustedReadContext:
    return TrustedReadContext(
        principal_id=READER,
        channel="whatsapp",
        chat_id=chat_id,
        recipient_principals=frozenset({READER, recipient, *additional_recipients}),
        membership_revision=membership_revision,
        policy_revision=policy_revision,
        purpose="reply",
        now_ms=NOW + 10_000,
        is_direct=is_direct,
    )


def _add_event(
    journal: HistoricalJournal,
    *,
    event_id: str = "wa-history-1",
    revision: int = 1,
    text: str = "Broker Stillhalter YTD 2025",
    native_id: str = "native-123",
    occurred_ms: int = NOW,
    account: str = "acct-primary",
    audience: tuple[str, ...] | None = (READER,),
    denied: bool = False,
    provenance: str = "native",
    source_authority: str = "inbound_archive_copy",
    copy_source_authority: str | None = None,
    payload_purged_ms: int | None = None,
    alias_event_id: str | None = None,
    chat_id: str = GROUP,
    chat_kind: str = "group",
    audience_status: str | None = None,
) -> None:
    text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    source_id = "bundle-events"
    locator = {"file": "events.jsonl", "line": 17}
    locator_json = json.dumps(locator, sort_keys=True, separators=(",", ":"))
    evidence_class = audience_status or ("known" if audience is not None else "unknown")
    if evidence_class == "author_only":
        native_evidence = [
            {
                "evidence_class": "author_only",
                "channel": "whatsapp",
                "account": account,
                "chat_id": chat_id,
                "author_principal": READER,
                "valid_from_ms": occurred_ms - 60_000,
                "valid_until_ms": occurred_ms + 60_000,
                "source_refs": ["author-source-proof"],
            }
        ]
    elif audience is not None:
        native_evidence = [
            {
                "evidence_class": "membership_snapshot",
                "channel": "whatsapp",
                "account": account,
                "chat_id": chat_id,
                "members": list(audience or ()),
                "valid_from_ms": occurred_ms - 60_000,
                "valid_until_ms": occurred_ms + 60_000,
                "source_refs": ["membership-snapshot-1"],
            }
        ]
    else:
        native_evidence = []
    normalized = {
        "normalization_version": 1,
        "event_id": event_id,
        "revision": revision,
        "channel": "whatsapp",
        "account": account,
        "chat_id": chat_id,
        "native_id": native_id,
        "kind": "message",
        "chat_kind": chat_kind,
        "direction": "in",
        "sender_raw": "reader@s.whatsapp.net",
        "principal": READER,
        "observed_ms": occurred_ms + 100,
        "occurred_ms": occurred_ms,
        "time_certainty": "provider_timestamp",
        "time_metadata": {"basis": "provider_timestamp"},
        "text": None if denied else text,
        "text_hash": text_hash,
        "media_kind": None,
        "media_missing": False,
        "reply_target": None,
        "edit_target": None,
        "delete_target": None,
        "source_id": source_id,
        "source_hash": "a" * 64,
        "locator": locator,
        "provenance_class": provenance,
        "source_authority": source_authority,
        "retention_status": "retained",
        "payload_purged_ms": payload_purged_ms,
        "native_evidence": native_evidence,
        "copies": [],
        "verbatim_unverified": False,
        "internal_secret": "must-never-be-rendered",
    }
    journal.store.append_event(
        event_key=f"history:{event_id}:{revision}",
        event_id=event_id,
        trace_id=f"trace:{event_id}:{revision}",
        payload={
            "kind": "message",
            "origin": "historical_rebuild",
            "channel": "whatsapp",
            "chat_id": chat_id,
            "account": account,
            "direction": "in",
            "revision": revision,
            "occurred_ms": occurred_ms,
            "source_message_id": native_id,
            "principal": READER,
            "text": None if denied else text,
        },
        now_ms=occurred_ms,
        account=account,
        direction="in",
        revision=revision,
    )
    journal.store.upsert_event_source_authority(
        source=SourceRef(
            event_id=event_id,
            revision=revision,
            channel="whatsapp",
            chat_id=chat_id,
            author_principal=READER,
            occurred_at_ms=occurred_ms,
        ),
        audience=(
            EvidenceAudience.author_only()
            if evidence_class == "author_only"
            else EvidenceAudience.known(frozenset(audience or ()))
            if evidence_class == "known"
            else EvidenceAudience.unknown()
        ),
        now_ms=occurred_ms,
    )
    copy_authority = copy_source_authority or source_authority
    copy_json = {
        "source_id": source_id,
        "locator": locator,
        "provenance_class": provenance,
        "source_authority": copy_authority,
        "raw_payload": {"secret": "must-never-be-rendered"},
    }
    with journal.store._write() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO history_meta (key,value) VALUES ('build_status','complete')"
        )
        connection.execute(
            "INSERT INTO history_event_details VALUES (?,?,?,?,?,?,?,?,?)",
            (
                event_id,
                str(revision),
                json.dumps(normalized, sort_keys=True),
                "message",
                "in",
                provenance,
                "retained",
                text_hash,
                int(denied),
            ),
        )
        connection.execute(
            "INSERT INTO history_event_copies "
            "(event_id,revision,source_id,source_hash,locator_json,source_kind,semantic_kind,"
            "semantic_direction,provenance_class,source_authority,channel,account,chat_id,"
            "native_id,text_hash,text_value,disposition,copy_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_id,
                str(revision),
                source_id,
                "a" * 64,
                locator_json,
                "jsonl",
                "message",
                "in",
                provenance,
                copy_authority,
                "whatsapp",
                account,
                chat_id,
                native_id,
                text_hash,
                None if denied else text,
                "denied" if denied else "logical_copy",
                json.dumps(copy_json, sort_keys=True),
            ),
        )
        connection.execute(
            "INSERT INTO history_event_aliases VALUES (?,?,?,?,?,?,?)",
            (
                alias_event_id or event_id,
                str(revision),
                event_id,
                str(revision),
                source_id,
                locator_json,
                "resolved",
            ),
        )
        for proof_event_id in dict.fromkeys((event_id, alias_event_id or event_id)):
            connection.execute(
                "INSERT INTO history_source_proofs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    proof_event_id,
                    str(revision),
                    source_id,
                    locator_json,
                    READER,
                    "whatsapp",
                    chat_id,
                    occurred_ms,
                    evidence_class,
                    json.dumps(
                        list(audience or ((READER,) if evidence_class == "author_only" else ()))
                    ),
                    "native-membership-1" if evidence_class != "unknown" else None,
                    "7",
                    None,
                    None,
                    int(not denied),
                    "denied" if denied else None,
                ),
            )


def _reader(
    journal: HistoricalJournal,
    *,
    members: set[str] | None = None,
    policy_revision: int = 7,
    registry: _Registry | None = None,
) -> Any:
    from yeoman_gateway.knowledge._history_reader import HistoryReader

    return HistoryReader(
        journal,
        policy=RuntimeKnowledgePolicy(
            engine=None,
            chat_registry=registry or _Registry(members),
            policy_revision=policy_revision,
        ),
    )


def _add_current_alias_event(
    journal: HistoricalJournal,
    *,
    event_id: str,
    account: str = "acct-primary",
    chat_id: str = GROUP,
) -> None:
    journal.store.append_event(
        event_key=f"retention-alias:{event_id}",
        event_id=event_id,
        trace_id=f"trace:{event_id}",
        payload={
            "kind": "message",
            "origin": "historical_rebuild",
            "channel": "whatsapp",
            "chat_id": chat_id,
            "account": account,
            "direction": "in",
            "revision": 1,
            "occurred_ms": NOW,
            "source_message_id": event_id,
            "principal": READER,
        },
        now_ms=NOW,
        account=account,
        direction="in",
        revision=1,
    )


def test_broker_stillhalter_ytd_search_by_date_and_id(tmp_path: Path) -> None:
    with HistoricalJournal(tmp_path / "history") as journal:
        _add_event(journal)
        reader = _reader(journal)

        results = reader.search(
            "Broker Stillhalter YTD",
            context=_context(),
            channel="whatsapp",
            account="acct-primary",
            chat_id=GROUP,
            since_ms=NOW - 1,
            until_ms=NOW + 1,
            native_id="native-123",
        )

        assert [item["event_id"] for item in results] == ["wa-history-1"]
        assert results[0]["text"] == "Broker Stillhalter YTD 2025"
        assert results[0]["occurred_ms"] == NOW
        assert results[0]["sources"][0]["locator"] == {"file": "events.jsonl", "line": 17}
        assert "must-never-be-rendered" not in json.dumps(results[0])


def test_recent_and_search_share_canonical_events(tmp_path: Path) -> None:
    with HistoricalJournal(tmp_path / "history") as journal:
        _add_event(journal)
        reader = _reader(journal)
        searched = reader.search(
            "broker",
            context=_context(),
            channel="whatsapp",
            account="acct-primary",
            chat_id=GROUP,
        )
        recent = reader.recent(
            context=_context(),
            channel="whatsapp",
            account="acct-primary",
            chat_id=GROUP,
            before_ms=NOW + 1,
        )

        assert [(row["event_id"], row["revision"], row["text"]) for row in searched] == [
            (row["event_id"], row["revision"], row["text"]) for row in recent
        ]


def test_revisions_and_excerpt_locators_resolve(tmp_path: Path) -> None:
    with HistoricalJournal(tmp_path / "history") as journal:
        _add_event(
            journal,
            event_id="canonical-v1",
            alias_event_id="provider-message",
            revision=1,
            text="Original broker note",
        )
        _add_event(
            journal,
            event_id="canonical-v2",
            alias_event_id="provider-message",
            revision=2,
            text="Revised Stillhalter note",
        )
        reader = _reader(journal)

        result = reader.excerpt("provider-message", 2, context=_context())

        assert result is not None
        assert (result["event_id"], result["revision"]) == ("canonical-v2", 2)
        assert (result["requested_event_id"], result["requested_revision"]) == (
            "provider-message",
            2,
        )
        assert result["text"] == "Revised Stillhalter note"
        assert result["sources"][0]["source_id"] == "bundle-events"
        assert result["sources"][0]["locator"] == {"file": "events.jsonl", "line": 17}
        assert reader.excerpt("provider-message", 3, context=_context()) is None
        results = reader.search(
            "note",
            context=_context(),
            channel="whatsapp",
            account="acct-primary",
            chat_id=GROUP,
        )
        assert {row["event_id"] for row in results} == {"canonical-v1", "canonical-v2"}


def test_denied_events_never_enter_index_excerpt_or_recent_window(tmp_path: Path) -> None:
    with HistoricalJournal(tmp_path / "history") as journal:
        _add_event(
            journal,
            event_id="deleted-event",
            native_id="deleted-native",
            text="purgedbrokersecret",
            denied=True,
        )
        reader = _reader(journal)

        report = reader.reindex()
        with journal.store._lock:
            indexed = journal.store._conn.execute(
                "SELECT COUNT(*) FROM history_search_fts WHERE normalized_text MATCH ?",
                ('"purgedbrokersecret"',),
            ).fetchone()[0]
        assert report["supported"] is True
        assert indexed == 0
        assert reader.search(
            "purgedbrokersecret",
            context=_context(),
            channel="whatsapp",
            account="acct-primary",
            chat_id=GROUP,
        ) == ()
        assert reader.recent(
            context=_context(),
            channel="whatsapp",
            account="acct-primary",
            chat_id=GROUP,
            before_ms=NOW + 1,
        ) == ()
        assert reader.excerpt("deleted-event", 1, context=_context()) is None


def test_current_canonical_and_compatible_alias_purges_block_all_read_paths(
    tmp_path: Path,
) -> None:
    with HistoricalJournal(tmp_path / "history") as journal:
        _add_event(journal, event_id="canonical-purged")
        _add_event(
            journal,
            event_id="alias-target",
            alias_event_id="purged-alias",
        )
        _add_current_alias_event(journal, event_id="purged-alias")
        with journal.store._write() as connection:
            connection.execute(
                "UPDATE events SET payload_purged_ms=? WHERE event_id IN (?,?)",
                (NOW + 1, "canonical-purged", "purged-alias"),
            )
        reader = _reader(journal)

        report = reader.reindex()
        assert report["indexed"] == 0
        assert reader.excerpt("canonical-purged", 1, context=_context()) is None
        assert reader.excerpt("purged-alias", 1, context=_context()) is None
        assert reader.search(
            "broker",
            context=_context(),
            channel="whatsapp",
            account="acct-primary",
            chat_id=GROUP,
        ) == ()
        assert reader.recent(
            context=_context(),
            channel="whatsapp",
            account="acct-primary",
            chat_id=GROUP,
            before_ms=NOW + 1,
        ) == ()
        authority = HistorySourceAuthority(journal)
        assert authority.verify_source_ref("canonical-purged", 1) is None
        assert authority.verify_source_ref("purged-alias", 1) is None


def test_cleared_current_marker_keeps_verified_whatsapp_restoration_readable(
    tmp_path: Path,
) -> None:
    with HistoricalJournal(tmp_path / "history") as journal:
        _add_event(
            journal,
            event_id="restored-event",
            source_authority="payload_purged",
            copy_source_authority="inbound_archive_copy",
            payload_purged_ms=NOW,
        )
        reader = _reader(journal)

        result = reader.excerpt("restored-event", 1, context=_context())
        index = reader.reindex()

        assert result is not None
        assert result["text"] == "Broker Stillhalter YTD 2025"
        assert index["indexed"] == 1


def test_unknown_audience_and_late_member_are_denied(tmp_path: Path) -> None:
    late = "whatsapp:late@s.whatsapp.net"
    with HistoricalJournal(tmp_path / "history") as journal:
        _add_event(journal, event_id="unknown-event", text="unknown audience word", audience=None)
        _add_event(journal, event_id="old-roster-event", text="old audience word", audience=(READER,))
        reader = _reader(journal, members={READER, late})

        assert reader.search(
            "unknown",
            context=_context(),
            channel="whatsapp",
            account="acct-primary",
            chat_id=GROUP,
        ) == ()
        assert reader.excerpt("old-roster-event", 1, context=_context(
            recipient=late, additional_recipients=(READER,)
        )) is None


def test_stale_policy_membership_and_unverified_recipient_fail_closed(tmp_path: Path) -> None:
    outsider = "whatsapp:outsider@s.whatsapp.net"
    with HistoricalJournal(tmp_path / "history") as journal:
        _add_event(journal)
        reader = _reader(journal)

        stale_policy = _context(policy_revision=6)
        stale_membership = _context(membership_revision="old-roster")
        unverified_recipient = _context(additional_recipients=(outsider,))
        for context in (stale_policy, stale_membership, unverified_recipient):
            assert reader.recent(
                context=context,
                channel="whatsapp",
                account="acct-primary",
                chat_id=GROUP,
                before_ms=NOW + 1,
            ) == ()


def _attest_reader(journal: HistoricalJournal, *, start: int = NOW - 1_000) -> tuple[str, RuntimeKnowledgePolicy]:
    policy = RuntimeKnowledgePolicy(
        engine=None,
        policy_revision=7,
        admin_principals=frozenset({READER}),
    )
    admin = TrustedAdminContext(READER, 7, "trusted-test-owner", owner=True)
    proof_id = HistoryAudience(journal).attest(
        channel="whatsapp",
        account="acct-primary",
        chat_id=GROUP,
        members=(READER,),
        valid_from_ms=start,
        valid_until_ms=NOW + 10_000,
        confirmation=roster_confirmation(
            channel="whatsapp",
            account="acct-primary",
            chat_id=GROUP,
            members=(READER,),
            valid_from_ms=start,
            valid_until_ms=NOW + 10_000,
        ),
        context=admin,
        policy=policy,
    )
    return proof_id, policy


def test_native_proof_survives_withdrawal_of_separate_roster(tmp_path: Path) -> None:
    with HistoricalJournal(tmp_path / "history") as journal:
        _add_event(journal)
        proof_id, owner_policy = _attest_reader(journal)
        class _RevokingRegistry(_Registry):
            def __init__(self) -> None:
                super().__init__()
                self.calls = 0

            def get_chat(self, channel: str, chat_id: str) -> dict[str, Any]:
                self.calls += 1
                if self.calls == 3:
                    HistoryAudience(journal).revoke(
                        proof_id,
                        context=TrustedAdminContext(
                            READER, 7, "synthetic-review-owner", owner=True
                        ),
                        policy=owner_policy,
                    )
                return super().get_chat(channel, chat_id)

        reader = _reader(journal, registry=_RevokingRegistry())

        result = reader.excerpt("wa-history-1", 1, context=_context())
        assert result is not None
        assert result["audience"]["evidence_class"] == "native_snapshot"


def test_revoked_roster_blocks_actual_derived_knowledge_context(tmp_path: Path) -> None:
    with HistoricalJournal(tmp_path / "history") as journal:
        _add_event(journal, audience=None)
        proof_id, owner_policy = _attest_reader(journal)

        class _RevokingRegistry(_Registry):
            def __init__(self) -> None:
                super().__init__()
                self.calls = 0
                self.revoke_on_call: int | None = None

            def get_chat(self, channel: str, chat_id: str) -> dict[str, Any]:
                self.calls += 1
                if self.calls == self.revoke_on_call:
                    HistoryAudience(journal).revoke(
                        proof_id,
                        context=TrustedAdminContext(
                            READER, 7, "synthetic-review-owner", owner=True
                        ),
                        policy=owner_policy,
                    )
                return super().get_chat(channel, chat_id)

        registry = _RevokingRegistry()
        authority = HistorySourceAuthority(journal)
        source = authority.verify_source_ref("wa-history-1", 1)
        assert source is not None
        policy = RuntimeKnowledgePolicy(
            engine=None,
            chat_registry=registry,
            policy_revision=7,
        )
        knowledge = open_knowledge_store(
            tmp_path / "knowledge.db",
            workspace_id=workspace_id_for(tmp_path),
            source_authority=authority,
            policy_authority=policy,
        )
        try:
            receipt = knowledge.capture(
                StatementCandidate(
                    content="Derived broker note from a roster-authorized message",
                    sources=(source,),
                    extractor_version="history-reader-test",
                    confidence=0.9,
                ),
                context=TrustedCaptureContext(
                    request_id="history-reader-test",
                    policy_revision=7,
                    capture_basis="history_test",
                    authorized_sources=(source,),
                ),
            )
            assert receipt.statement_ids
            prepared = knowledge.recall(
                RecallQuery(text="derived broker note", limit=5), context=_context()
            )
            assert isinstance(prepared, KnowledgeContext)
            assert not prepared.empty
            assert "Derived broker note" in prepared.text

            with journal.store._write() as connection:
                connection.execute(
                    "UPDATE events SET payload_purged_ms=? WHERE event_id=?",
                    (NOW + 1, "wa-history-1"),
                )
            assert authority.verify_source_ref("wa-history-1", 1) is None
            retention_checked = _reader(journal, registry=registry).revalidate(
                prepared,
                knowledge=knowledge,
                context=_context(),
            )
            assert retention_checked.empty

            with journal.store._write() as connection:
                connection.execute(
                    "UPDATE events SET payload_purged_ms=NULL WHERE event_id=?",
                    ("wa-history-1",),
                )
            registry.calls = 0
            registry.revoke_on_call = 3
            checked = _reader(journal, registry=registry).revalidate(
                prepared, knowledge=knowledge, context=_context()
            )

            assert checked.empty
            assert checked.text == ""
            assert registry.calls >= 3
        finally:
            knowledge.close()


def test_proof_change_during_read_is_rechecked_before_output(tmp_path: Path) -> None:
    class _RevokingRegistry(_Registry):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0
            self.journal: HistoricalJournal | None = None
            self.proof_id = ""
            self.admin_policy: RuntimeKnowledgePolicy | None = None

        def get_chat(self, channel: str, chat_id: str) -> dict[str, Any]:
            self.calls += 1
            if self.calls == 3:
                assert self.journal is not None and self.admin_policy is not None
                HistoryAudience(self.journal).revoke(
                    self.proof_id,
                    context=TrustedAdminContext(READER, 7, "trusted-test-owner", owner=True),
                    policy=self.admin_policy,
                )
            return super().get_chat(channel, chat_id)

    with HistoricalJournal(tmp_path / "history") as journal:
        _add_event(journal, audience=None)
        proof_id, owner_policy = _attest_reader(journal)
        registry = _RevokingRegistry()
        registry.journal = journal
        registry.proof_id = proof_id
        registry.admin_policy = owner_policy
        reader = _reader(journal, registry=registry)

        assert reader.search(
            "broker",
            context=_context(),
            channel="whatsapp",
            account="acct-primary",
            chat_id=GROUP,
        ) == ()


def test_earlier_result_proof_withdrawn_during_multi_result_read(tmp_path: Path) -> None:
    class _RevokingRegistry(_Registry):
        def __init__(self, journal: HistoricalJournal, proof_id: str, policy: RuntimeKnowledgePolicy):
            super().__init__()
            self.calls = 0
            self.journal = journal
            self.proof_id = proof_id
            self.admin_policy = policy

        def get_chat(self, channel: str, chat_id: str) -> dict[str, Any]:
            self.calls += 1
            if self.calls == 5:
                HistoryAudience(self.journal).revoke(
                    self.proof_id,
                    context=TrustedAdminContext(
                        READER, 7, "synthetic-review-owner", owner=True
                    ),
                    policy=self.admin_policy,
                )
            return super().get_chat(channel, chat_id)

    with HistoricalJournal(tmp_path / "history") as journal:
        _add_event(journal, event_id="first-event", audience=None)
        _add_event(journal, event_id="second-event", audience=None)
        proof_id, owner_policy = _attest_reader(journal)
        registry = _RevokingRegistry(journal, proof_id, owner_policy)
        reader = _reader(journal, registry=registry)

        results = reader.search(
            "broker",
            context=_context(),
            channel="whatsapp",
            account="acct-primary",
            chat_id=GROUP,
        )

        assert registry.calls >= 5
        assert results == ()


def test_cross_account_scope_and_limit_apply_after_authorization(tmp_path: Path) -> None:
    with HistoricalJournal(tmp_path / "history") as journal:
        _add_event(journal, event_id="a-unknown", audience=None)
        _add_event(
            journal,
            event_id="b-older-authorized",
            occurred_ms=NOW - 100,
            audience=(READER,),
        )
        _add_event(journal, event_id="z-newer-authorized", audience=(READER,))
        _add_event(
            journal,
            event_id="other-account",
            account="acct-secondary",
            audience=(READER,),
        )
        reader = _reader(journal)

        primary = reader.search(
            "broker",
            context=_context(),
            channel="whatsapp",
            account="acct-primary",
            chat_id=GROUP,
            limit=1,
        )
        secondary = reader.search(
            "broker",
            context=_context(),
            channel="whatsapp",
            account="acct-secondary",
            chat_id=GROUP,
            limit=1,
        )

        assert [row["event_id"] for row in primary] == ["z-newer-authorized"]
        assert [row["account"] for row in primary] == ["acct-primary"]
        assert [row["event_id"] for row in secondary] == ["other-account"]


def test_author_only_proof_is_limited_to_exact_author_direct_context(tmp_path: Path) -> None:
    with HistoricalJournal(tmp_path / "history") as journal:
        _add_event(
            journal,
            event_id="direct-event",
            chat_id=DIRECT,
            chat_kind="direct",
            audience=None,
            audience_status="author_only",
        )
        reader = _reader(journal, registry=_Registry(chat_id=DIRECT))

        allowed = reader.excerpt(
            "direct-event",
            1,
            context=_context(chat_id=DIRECT, is_direct=True),
        )
        group_context = reader.excerpt(
            "direct-event",
            1,
            context=_context(chat_id=DIRECT, is_direct=False),
        )
        extra_recipient = reader.excerpt(
            "direct-event",
            1,
            context=_context(
                chat_id=DIRECT,
                is_direct=True,
                additional_recipients=("whatsapp:other@s.whatsapp.net",),
            ),
        )

        assert allowed is not None and allowed["event_id"] == "direct-event"
        assert group_context is None
        assert extra_recipient is None
