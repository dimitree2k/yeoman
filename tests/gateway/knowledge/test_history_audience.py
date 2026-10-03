from __future__ import annotations

import json
import sqlite3
import stat
from pathlib import Path

import pytest
from yeoman_gateway.knowledge._history import HistoricalJournal, HistoryTargetError
from yeoman_gateway.knowledge._history_audience import HistoryAudience, roster_confirmation
from yeoman_gateway.knowledge.models import KnowledgeError, TrustedAdminContext
from yeoman_gateway.knowledge.runtime import RuntimeKnowledgePolicy


def _journal(tmp_path: Path) -> HistoricalJournal:
    return HistoricalJournal(tmp_path / "history-target")


def _context(*, owner: bool = True) -> TrustedAdminContext:
    return TrustedAdminContext(
        actor_principal="whatsapp:owner",
        policy_revision=7,
        authorization_ref="policy:history-test",
        owner=owner,
    )


def _policy() -> RuntimeKnowledgePolicy:
    return RuntimeKnowledgePolicy(
        engine=None,
        policy_revision=7,
        admin_principals=frozenset({"whatsapp:owner"}),
    )


def _event(at: int | None) -> dict[str, object]:
    return {
        "event_id": "event-1",
        "revision": 1,
        "channel": "whatsapp",
        "account": "account-1",
        "chat_id": "group-1",
        "occurred_ms": at,
        "time_certainty": "exact" if at is not None else "unknown",
        "sender_raw": "whatsapp:sender-proposal",
        "principal": "whatsapp:unverified-proposal",
    }


def _snapshot(members: list[str], start: int, end: int) -> dict[str, object]:
    return {
        "evidence_class": "native_snapshot",
        "members": members,
        "source_refs": [
            {
                "source_id": "archive",
                "table": "canonical_events",
                "event_id": "event-1",
                "revision": 1,
            }
        ],
        "valid_from_ms": start,
        "valid_until_ms": end,
    }


def test_attestation_requires_owner_and_exact_confirmation(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    audience = HistoryAudience(journal)
    policy = _policy()
    members = {"whatsapp:owner", "whatsapp:alice"}
    confirmation = roster_confirmation(
        channel="whatsapp", account="account-1", chat_id="group-1", members=members,
        valid_from_ms=100, valid_until_ms=200,
    )

    with pytest.raises(KnowledgeError, match="owner"):
        audience.attest(
            channel="whatsapp",
            account="account-1",
            chat_id="group-1",
            members=members,
            valid_from_ms=100,
            valid_until_ms=200,
            confirmation=confirmation,
            context=_context(owner=False),
            policy=policy,
        )
    with pytest.raises(KnowledgeError, match="administrator"):
        audience.attest(
            channel="whatsapp",
            account="account-1",
            chat_id="group-1",
            members=members,
            valid_from_ms=100,
            valid_until_ms=200,
            confirmation=confirmation,
            context=TrustedAdminContext(
                actor_principal="whatsapp:intruder",
                policy_revision=7,
                authorization_ref="policy:history-test",
                owner=True,
            ),
            policy=policy,
        )
    with pytest.raises(KnowledgeError, match="confirmation"):
        audience.attest(
            channel="whatsapp",
            account="account-1",
            chat_id="group-1",
            members=members,
            valid_from_ms=100,
            valid_until_ms=200,
            confirmation="yes",
            context=_context(),
            policy=policy,
        )
    with pytest.raises(KnowledgeError, match="confirmation"):
        audience.attest(
            channel="whatsapp",
            account="account-1",
            chat_id="group-1",
            members=members,
            valid_from_ms=100,
            valid_until_ms=201,
            confirmation=confirmation,
            context=_context(),
            policy=policy,
        )

    assert journal.store._conn.execute("SELECT COUNT(*) FROM history_audience_proofs").fetchone()[0] == 0
    journal.close()


def test_attestation_half_open_period_and_unknown_time(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    audience = HistoryAudience(journal)
    members = {"whatsapp:owner", "whatsapp:alice"}
    proof_id = audience.attest(
        channel="whatsapp",
        account="account-1",
        chat_id="group-1",
        members=members,
        valid_from_ms=100,
        valid_until_ms=200,
        confirmation=roster_confirmation(
            channel="whatsapp", account="account-1", chat_id="group-1", members=members,
            valid_from_ms=100, valid_until_ms=200,
        ),
        context=_context(),
        policy=_policy(),
    )

    assert audience.resolve(_event(100)).members == frozenset(members)
    assert audience.resolve(_event(199)).members == frozenset(members)
    assert audience.resolve(_event(200)).status == "unknown"
    assert audience.resolve(_event(None)).status == "unknown"
    assert audience.resolve(_event(99)).proof_id != proof_id
    journal.close()


def test_attestation_revoke_restores_unknown(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    audience = HistoryAudience(journal)
    members = {"whatsapp:owner", "whatsapp:alice"}
    proof_id = audience.attest(
        channel="whatsapp",
        account="account-1",
        chat_id="group-1",
        members=members,
        valid_from_ms=100,
        valid_until_ms=200,
        confirmation=roster_confirmation(
            channel="whatsapp", account="account-1", chat_id="group-1", members=members,
            valid_from_ms=100, valid_until_ms=200,
        ),
        context=_context(),
        policy=_policy(),
    )

    audience.revoke(proof_id, context=_context(), policy=_policy())

    assert audience.resolve(_event(150)).status == "unknown"
    journal.close()


def test_no_roster_from_senders_counts_or_current_members(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    audience = HistoryAudience(journal)
    event = _event(150)
    event["native_evidence"] = [
        {
            "evidence_class": "native_snapshot",
            "status": "known",
            "source_refs": ["archive:status-only"],
            "valid_from_ms": 100,
            "valid_until_ms": 200,
        },
        {"evidence_class": "sender_count", "count": 12},
    ]

    proof = audience.resolve(event)
    review = audience.roster_review(
        [event], current_members={"whatsapp:account-1:group-1": ["whatsapp:current"]}
    )

    assert proof.status == "unknown"
    assert proof.members == frozenset()
    assert review["chats"][0]["roster"]["status"] == "unknown"
    assert review["chats"][0]["current_members"]["available"] is True
    assert "whatsapp:current" not in review["chats"][0]["roster"].get("members", [])
    journal.close()


@pytest.mark.parametrize(
    "source_refs",
    [
        [None],
        [""],
        [{}],
        [0],
        [True],
        [{"other": "value"}],
        [{"source_id": ""}],
        [{"revision": 1}],
        [{"table": "canonical_events"}],
        [{"event_id": 1}],
    ],
)
def test_placeholder_source_refs_do_not_prove_known_audience(
    tmp_path: Path, source_refs: list[object]
) -> None:
    journal = _journal(tmp_path)
    audience = HistoryAudience(journal)
    snapshot = _snapshot(["whatsapp:alice"], 100, 200)
    snapshot["source_refs"] = source_refs

    proof = audience.resolve(_event(150), native_evidence=[snapshot])
    journal.close()

    assert proof.status == "unknown"
    assert proof.members == frozenset()


def test_native_proof_preserves_meaningful_string_source_ref(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    audience = HistoryAudience(journal)
    snapshot = _snapshot(["whatsapp:alice"], 100, 200)
    snapshot["source_refs"] = ["archive:canonical_events:event-1:1"]

    proof = audience.resolve(_event(150), native_evidence=[snapshot])
    journal.close()

    assert proof.status == "known"
    assert proof.source_refs == ("archive:canonical_events:event-1:1",)


def test_native_proof_preserves_structured_source_locator(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    audience = HistoryAudience(journal)
    reference = {
        "source_id": "archive",
        "table": "canonical_events",
        "event_id": "event-1",
        "revision": 1,
    }
    snapshot = _snapshot(["whatsapp:alice"], 100, 200)
    snapshot["source_refs"] = [reference]

    proof = audience.resolve(_event(150), native_evidence=[snapshot])
    journal.close()

    assert proof.status == "known"
    assert proof.source_refs == (reference,)


def test_partial_membership_timeline_requires_complete_anchor(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    audience = HistoryAudience(journal)
    event = _event(150)
    change = {
        "evidence_class": "membership_add",
        "member": "whatsapp:bob",
        "source_refs": [
            {
                "source_id": "registry",
                "table": "membership_events",
                "event_id": "add-1",
                "revision": 1,
            }
        ],
        "valid_from_ms": 120,
        "valid_until_ms": 180,
    }

    assert audience.resolve(event, native_evidence=[change]).status == "unknown"
    proof = audience.resolve(
        event,
        native_evidence=[
            _snapshot(["whatsapp:owner", "whatsapp:alice"], 100, 200),
            change,
        ],
    )

    assert proof.status == "known"
    assert proof.members == frozenset({"whatsapp:owner", "whatsapp:alice", "whatsapp:bob"})
    assert proof.source_refs == (
        {
            "source_id": "archive",
            "table": "canonical_events",
            "event_id": "event-1",
            "revision": 1,
        },
        {
            "source_id": "registry",
            "table": "membership_events",
            "event_id": "add-1",
            "revision": 1,
        },
    )
    journal.close()


def test_known_native_proof_not_widened_by_attestation(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    audience = HistoryAudience(journal)
    native_members = {"whatsapp:owner", "whatsapp:alice"}
    event = _event(99)
    native = _snapshot(sorted(native_members), 0, 100)
    audience.attest(
        channel="whatsapp",
        account="account-1",
        chat_id="group-1",
        members=native_members | {"whatsapp:bob"},
        valid_from_ms=100,
        valid_until_ms=200,
        confirmation=roster_confirmation(
            channel="whatsapp",
            account="account-1",
            chat_id="group-1",
            members=native_members | {"whatsapp:bob"},
            valid_from_ms=100,
            valid_until_ms=200,
        ),
        context=_context(),
        policy=_policy(),
    )

    proof = audience.resolve(event, native_evidence=[native])

    assert proof.evidence_class == "native_snapshot"
    assert proof.members == frozenset(native_members)
    journal.close()


def test_overlapping_inconsistent_proofs_fail_closed(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    audience = HistoryAudience(journal)
    native_members = {"whatsapp:owner", "whatsapp:alice"}
    attested_members = {"whatsapp:owner", "whatsapp:bob"}
    audience.attest(
        channel="whatsapp",
        account="account-1",
        chat_id="group-1",
        members=attested_members,
        valid_from_ms=50,
        valid_until_ms=150,
        confirmation=roster_confirmation(
            channel="whatsapp", account="account-1", chat_id="group-1", members=attested_members,
            valid_from_ms=50, valid_until_ms=150,
        ),
        context=_context(),
        policy=_policy(),
    )

    proof = audience.resolve(
        _event(75), native_evidence=[_snapshot(sorted(native_members), 0, 100)]
    )

    assert proof.status == "unknown"
    assert proof.members == frozenset()
    journal.close()


def test_history_target_rejects_live_overlap_symlink_or_unmarked_db(tmp_path: Path) -> None:
    protected = tmp_path / "live"
    protected.mkdir()
    with pytest.raises(HistoryTargetError):
        HistoricalJournal(protected / "inside", protected_home=protected)

    actual = tmp_path / "actual"
    actual.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(actual, target_is_directory=True)
    with pytest.raises(HistoryTargetError):
        HistoricalJournal(link)

    unmarked = tmp_path / "unmarked"
    (unmarked / "data").mkdir(parents=True)
    (unmarked / "data" / "processing.db").write_bytes(b"not a history journal")
    with pytest.raises(HistoryTargetError):
        HistoricalJournal(unmarked)
    assert (unmarked / "data" / "processing.db").read_bytes() == b"not a history journal"

    marked = tmp_path / "marked"
    with HistoricalJournal(marked):
        pass
    external_file = tmp_path / "external"
    external_file.write_bytes(b"must stay unchanged")
    (marked / "data" / "processing.db-wal").symlink_to(external_file)
    with pytest.raises(HistoryTargetError):
        HistoricalJournal(marked, create=False)
    assert external_file.read_bytes() == b"must stay unchanged"


def test_mismatched_marked_database_is_rejected_without_modification(
    tmp_path: Path,
) -> None:
    target = tmp_path / "mismatched"
    data = target / "data"
    data.mkdir(parents=True)
    database = data / "processing.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE unrelated (value TEXT NOT NULL)")
        connection.execute("INSERT INTO unrelated VALUES ('keep me')")
    marker = target / ".history-journal.json"
    marker.write_text(
        json.dumps(
            {
                "format": "yeoman-history-journal-v1",
                "database": "data/processing.db",
                "journal_id": "plausible-but-unowned",
            }
        ),
        encoding="utf-8",
    )

    sidecars = [Path(f"{database}{suffix}") for suffix in ("-wal", "-shm")]
    sidecars[0].write_bytes(b"existing wal bytes")
    sidecars[1].write_bytes(b"existing shm bytes")
    paths = [database, marker, *sidecars, Path(f"{database}-journal")]
    before_files = {path: path.read_bytes() for path in paths if path.exists()}
    before_modes = {
        path: stat.S_IMODE(path.stat().st_mode)
        for path in (target, data, *before_files)
    }

    with pytest.raises(HistoryTargetError, match="marked history database"):
        HistoricalJournal(target, create=False)

    assert {path: path.read_bytes() for path in before_files} == before_files
    assert {path for path in paths if path.exists()} == set(before_files)
    assert {
        path: stat.S_IMODE(path.stat().st_mode)
        for path in (target, data, *before_files)
    } == before_modes
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall() == [("unrelated",)]
        assert connection.execute("SELECT value FROM unrelated").fetchall() == [
            ("keep me",)
        ]


def test_history_journal_reopens_only_its_marked_target(tmp_path: Path) -> None:
    target = tmp_path / "history-target"
    with HistoricalJournal(target) as created:
        journal_id = created.store._conn.execute(
            "SELECT value FROM history_meta WHERE key = 'journal_id'"
        ).fetchone()[0]

    with HistoricalJournal(target, create=False) as reopened:
        reopened_id = reopened.store._conn.execute(
            "SELECT value FROM history_meta WHERE key = 'journal_id'"
        ).fetchone()[0]

    assert journal_id == reopened_id
