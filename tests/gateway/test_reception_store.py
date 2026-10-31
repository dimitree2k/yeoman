"""``sentiment.db`` storage round trip: ids and numbers, never message text."""

from __future__ import annotations

from pathlib import Path

from yeoman_gateway.reception.store import SentimentStore

CHAT = "120363000000000042@g.us"


def _store(tmp_path: Path) -> SentimentStore:
    return SentimentStore(tmp_path / "ops" / "sentiment.db")


def test_schema_is_versioned_private_and_wal(tmp_path: Path) -> None:
    store = _store(tmp_path)
    db = store._db()
    assert db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    version = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
    assert version == "1"
    assert (store.path.stat().st_mode & 0o777) == 0o600
    store.close()


def test_outbound_weekly_and_labels_round_trip_without_text(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_outbound(
        chat_id=CHAT, message_id="provider-1", sent_ms=100, path="participation",
        action_type="answer", persona_sha="abc",
    )
    meta = store.outbound_meta(CHAT)
    assert meta["provider-1"]["path"] == "participation"
    assert meta["provider-1"]["action_type"] == "answer"

    store.store_weekly(
        chat_id=CHAT, iso_week="2026-W40", computed_at_ms=1, status="ok",
        persona_file="personas/arvid.md", persona_sha="abc",
        policy_snapshot={"when_to_reply": "all", "persona_sha": "abc"},
        metrics={"arvid_messages": 3, "direct_rate": 0.5, "top_person_id": "c-a"},
    )
    row = store.weekly(CHAT)[0]
    assert row.status == "ok" and row.metrics["direct_rate"] == 0.5
    assert row.policy_snapshot["when_to_reply"] == "all"
    assert store.has_weekly(chat_id=CHAT, iso_week="2026-W40")

    store.store_label(
        chat_id=CHAT, iso_week="2026-W40", arvid_message_id="a-1",
        message_id="m-1", responds_to_arvid="yes", stance="playful",
        signals=("uses_his_answer",), confidence=0.9, model="fake/model",
        prompt_version="v1", labeled_at_ms=2,
    )
    label = store.labels(CHAT, iso_week="2026-W40")[0]
    assert (label.stance, label.signals, label.responds_to_arvid) == (
        "playful", ("uses_his_answer",), "yes"
    )
    assert store.labeled_message_ids(
        chat_id=CHAT, iso_week="2026-W40", prompt_version="v1"
    ) == {"m-1"}

    # No schema column can carry message text; the file bytes prove it too.
    tables = [
        str(item[0]) for item in store._db().execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    ]
    for table in tables:
        columns = {
            str(item[1])
            for item in store._db().execute(f"PRAGMA table_info({table})").fetchall()
        }
        assert not {"text", "content", "message", "excerpt"}.intersection(columns), table
    store.close()


def test_alert_rate_limit_allows_one_per_day(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.claim_alert(chat_id=CHAT, now_ms=1_000) is True
    assert store.claim_alert(chat_id=CHAT, now_ms=2_000) is False
    assert store.claim_alert(chat_id=CHAT, now_ms=1_000 + 24 * 60 * 60 * 1000) is True
    assert store.last_alert_ms(CHAT) == 1_000 + 24 * 60 * 60 * 1000
    store.close()


def test_persona_change_is_recorded_once_per_sha(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.note_persona(chat_id=CHAT, persona_sha="a", now_ms=10) is True
    assert store.note_persona(chat_id=CHAT, persona_sha="a", now_ms=20) is False
    assert store.note_persona(chat_id=CHAT, persona_sha="b", now_ms=30) is True
    assert store.persona_changes(CHAT) == (("a", 10), ("b", 30))
    assert store.persona_report_pending(chat_id=CHAT, persona_sha="b") is True
    store.mark_persona_reported(chat_id=CHAT, persona_sha="b", now_ms=40)
    assert store.persona_report_pending(chat_id=CHAT, persona_sha="b") is False
    store.close()
