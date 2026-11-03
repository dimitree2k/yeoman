"""Deterministic stance notes: counts, thresholds, TTL, family isolation and windows."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from yeoman_gateway.history.queries import HistoryQueries
from yeoman_gateway.knowledge._person_notes import PersonNoteStore
from yeoman_gateway.knowledge._store import KnowledgeStore
from yeoman_gateway.knowledge.models import PersonNoteLine, PersonNoteSeed, PersonNoteView
from yeoman_gateway.reception.stance import (
    STANCE_BLOCK_HEADER,
    ReceptionStanceBuilder,
    StanceBounds,
    StanceCounts,
    StanceNotesReader,
    build_patterns,
    dedupe_reply_labels,
    note_is_renderable,
    render_stance_block,
)
from yeoman_gateway.reception.store import LabelRow, SentimentStore

from tests.gateway.history_test_helpers import ProjectedHistory

CHAT = "120363000000000001@g.us"
BASE = 1_700_000_000
PHONE = "491510000001"
OTHER = "491510000002"


def _label(
    message_id: str,
    *,
    stance: str,
    responds: str = "yes",
    confidence: float = 0.9,
    signals: tuple[str, ...] = (),
    labeled_at_ms: int = BASE * 1000,
) -> LabelRow:
    return LabelRow(
        chat_id=CHAT,
        iso_week="2026-W01",
        arvid_message_id="a",
        message_id=message_id,
        responds_to_arvid=responds,
        stance=stance,
        signals=signals,
        confidence=confidence,
        model="synthetic",
        prompt_version="v",
        labeled_at_ms=labeled_at_ms,
    )


def _history(tmp_path: Path, *, people: int = 2) -> ProjectedHistory:
    history = ProjectedHistory(tmp_path / "history")
    phones = [
        PHONE,
        OTHER,
        "491510000003",
        "491510000004",
        "491510000005",
        "491510000006",
        "491510000007",
    ][:people]
    history.add_group(
        chat_id=CHAT,
        participants=[f"{phone}@s.whatsapp.net" for phone in phones],
        timestamp=BASE,
    )
    for index, phone in enumerate(phones):
        history.add_contact(
            identifiers=[f"{phone}@s.whatsapp.net"],
            name=f"Person {index + 1}",
            timestamp=BASE,
        )
        history.add_message(
            chat_id=CHAT,
            message_id=f"m{index}",
            text="synthetic",
            timestamp=BASE + index * 86_400,
            sender_id=f"{phone}@s.whatsapp.net",
        )
    return history


async def _message_ids(history: ProjectedHistory) -> dict[str, str]:
    async with history.turn() as snapshot:
        rows = HistoryQueries(snapshot).recent(chat_id=CHAT, limit=50)
    return {str(row["native_message_id"]): str(row["message_id"]) for row in rows}


def _knowledge(tmp_path: Path) -> tuple[KnowledgeStore, PersonNoteStore]:
    store = KnowledgeStore(tmp_path / "knowledge.db")
    return store, PersonNoteStore(store, workspace_id="default")


def test_duplicate_labels_for_one_reply_count_once() -> None:
    labels = [
        _label("m1", stance="neutral", responds="no", confidence=0.99),
        _label("m1", stance="hostile", responds="yes", confidence=0.4),
        _label("m1", stance="playful", responds="yes", confidence=0.8),
        _label("m2", stance="annoyed"),
    ]
    chosen = dedupe_reply_labels(labels)
    assert set(chosen) == {"m1", "m2"}
    assert chosen["m1"].stance == "playful"  # strongest responds, then confidence


def test_patterns_need_minimum_replies_and_days() -> None:
    bounds = StanceBounds()
    too_few = StanceCounts(person_id="p", person_name="Tom", replies=2, hostile=2)
    too_few.days = {"2026-01-01", "2026-01-02"}
    assert build_patterns(too_few, bounds) == ()
    one_day = StanceCounts(person_id="p", person_name="Tom", replies=4, hostile=4)
    one_day.days = {"2026-01-01"}
    assert build_patterns(one_day, bounds) == ()
    enough = StanceCounts(person_id="p", person_name="Tom", replies=7, hostile=5)
    enough.days = {"2026-01-01", "2026-01-02"}
    assert build_patterns(enough, bounds)[0].startswith("often hostile toward Arvid (5 of 7")


def test_at_most_two_patterns_negative_first() -> None:
    counts = StanceCounts(
        person_id="p",
        person_name="Tom",
        replies=10,
        hostile=6,
        annoyed=4,
        playful=8,
        positive=9,
        signals={"task_request": 5, "uses_his_answer": 4},
    )
    counts.days = {"2026-01-01", "2026-01-02"}
    patterns = build_patterns(counts, StanceBounds())
    assert len(patterns) == 2
    assert patterns[0].startswith("often hostile")
    assert patterns[1].startswith("teases him playfully")


def test_ttl_uses_newest_evidence() -> None:
    view = PersonNoteView(
        note_id="n",
        channel="whatsapp",
        chat_id=CHAT,
        person_id="p",
        status="active",
        lines=(PersonNoteLine(kind="stance", text="Tom: hostile"),),
        sources=(("message", "m1", BASE * 1000),),
        family="stance",
    )
    assert note_is_renderable(view, now_ms=(BASE + 10 * 86_400) * 1000) is True
    assert note_is_renderable(view, now_ms=(BASE + 31 * 86_400) * 1000) is False
    assert (
        note_is_renderable(view, now_ms=(BASE + 31 * 86_400) * 1000, max_age_ms=32 * 86_400_000)
        is True
    )


def test_block_is_dropped_whole_when_over_budget() -> None:
    block = render_stance_block(["Tom: often hostile toward Arvid (5 of 7)"])
    assert STANCE_BLOCK_HEADER in block
    assert render_stance_block(["Tom: x"], max_chars=10) == ""


@pytest.mark.asyncio
async def test_builder_counts_and_stores_stance_family(tmp_path: Path) -> None:
    history = _history(tmp_path, people=1)
    for index in range(1, 5):
        history.add_message(
            chat_id=CHAT,
            message_id=f"m{index}",
            text="synthetic",
            timestamp=BASE + index * 86_400,
            sender_id=f"{PHONE}@s.whatsapp.net",
        )
    ids = await _message_ids(history)
    sentiment = SentimentStore(tmp_path / "ops" / "sentiment.db")
    knowledge, notes = _knowledge(tmp_path)
    try:
        for index in range(5):
            sentiment.store_label(
                chat_id=CHAT,
                iso_week="2026-W01",
                arvid_message_id="a",
                message_id=ids[f"m{index}"],
                responds_to_arvid="yes",
                stance="hostile" if index < 3 else "neutral",
                signals=("task_request",) if index == 4 else (),
                confidence=0.9,
                model="m",
                prompt_version="v",
                labeled_at_ms=(BASE + index * 86_400) * 1000,
            )
        # A second label for the same reply must not double its count: the strongest
        # responds wins even when the competing label has higher confidence.
        sentiment.store_label(
            chat_id=CHAT,
            iso_week="2026-W01",
            arvid_message_id="b",
            message_id=ids["m0"],
            responds_to_arvid="no",
            stance="neutral",
            signals=(),
            confidence=0.99,
            model="m",
            prompt_version="v2",
            labeled_at_ms=BASE * 1000,
        )
        builder = ReceptionStanceBuilder(
            store=sentiment,
            history=history,
            note_store=notes,
            bounds=StanceBounds(),
        )
        report = await builder.build(CHAT, now_ms=(BASE + 5 * 86_400) * 1000)
        assert report.built == 1
        stance = notes.notes(channel="whatsapp", chat_id=CHAT, family="stance")
        assert len(stance) == 1
        assert stance[0].family == "stance"
        assert stance[0].lines[0].text.startswith("Person 1: often hostile")
        assert "(3 of 5" in stance[0].lines[0].text
        # An unchanged hash is not stored again.
        again = await builder.build(CHAT, now_ms=(BASE + 5 * 86_400) * 1000)
        assert again.built == 0 and again.unchanged == 1
    finally:
        sentiment.close()
        knowledge.close()
        history.close()


def test_family_isolation_and_purge_cover_both(tmp_path: Path) -> None:
    knowledge, notes = _knowledge(tmp_path)
    try:
        profile = notes.store_note(
            PersonNoteSeed(
                channel="whatsapp",
                chat_id=CHAT,
                person_id="p",
                input_hash="profile-hash",
                input_cut_ms=1,
                lines=(PersonNoteLine(kind="observed", text="likes charts"),),
            )
        )
        stance = notes.store_note(
            PersonNoteSeed(
                channel="whatsapp",
                chat_id=CHAT,
                person_id="p",
                input_hash="stance-hash",
                input_cut_ms=1,
                lines=(PersonNoteLine(kind="stance", text="Tom: hostile"),),
                family="stance",
            )
        )
        assert (
            notes.latest(channel="whatsapp", chat_id=CHAT, person_id="p").note_id == profile.note_id
        )
        assert (
            notes.latest(channel="whatsapp", chat_id=CHAT, person_id="p", family="stance").note_id
            == stance.note_id
        )
        assert notes.has_hash(
            channel="whatsapp",
            chat_id=CHAT,
            person_id="p",
            input_hash="stance-hash",
            family="stance",
        )
        assert not notes.has_hash(
            channel="whatsapp", chat_id=CHAT, person_id="p", input_hash="stance-hash"
        )
        assert {item["family"] for item in notes.counts(channel="whatsapp")} == {"profile"}
        assert len(notes.counts(channel="whatsapp", family="stance")) == 1
        assert notes.purge(channel="whatsapp", chat_id=CHAT) == 2
    finally:
        knowledge.close()


def test_existing_person_note_table_migrates_to_profile_family(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path / "knowledge.db")
    try:
        with store.transaction():
            store.connection.execute(
                "CREATE TABLE knowledge_person_notes ("
                "note_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, channel TEXT NOT NULL,"
                " chat_id TEXT NOT NULL, person_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',"
                " input_hash TEXT NOT NULL, built_ms INTEGER NOT NULL, input_cut_ms INTEGER NOT NULL DEFAULT 0,"
                " lines_json TEXT NOT NULL DEFAULT '[]', superseded_by TEXT, stale_reason TEXT)"
            )
            store.connection.execute(
                "INSERT INTO knowledge_person_notes (note_id, workspace_id, channel, chat_id,"
                " person_id, status, input_hash, built_ms) VALUES ('old','w','whatsapp',?, 'p',"
                " 'active','h',1)",
                (CHAT,),
            )
        store.ensure_person_note_schema()
        row = store.query_one("SELECT family FROM knowledge_person_notes WHERE note_id='old'", ())
        assert row is not None and str(row["family"]) == "profile"
        columns = {
            str(item["name"])
            for item in store.query("PRAGMA table_info(knowledge_person_notes)", ())
        }
        assert "family" in columns
    finally:
        store.close()


def test_message_source_invalidation_marks_stance_note_stale(tmp_path: Path) -> None:
    knowledge, notes = _knowledge(tmp_path)
    try:
        notes.store_note(
            PersonNoteSeed(
                channel="whatsapp",
                chat_id=CHAT,
                person_id="p",
                input_hash="h",
                input_cut_ms=1,
                lines=(PersonNoteLine(kind="stance", text="Tom: hostile"),),
                sources=(("message", "m1", BASE * 1000),),
                family="stance",
            )
        )
        changed = notes.mark_stale([("message", "m1")], reason="source_delete")
        assert changed == 1
        view = notes.latest(channel="whatsapp", chat_id=CHAT, person_id="p", family="stance")
        assert view is not None and view.status == "stale"
        assert view.stale_reason == "source_delete" and view.lines == ()
    finally:
        knowledge.close()


@pytest.mark.asyncio
async def test_reader_only_includes_people_present_and_caps_at_six(tmp_path: Path) -> None:
    history = _history(tmp_path, people=7)
    knowledge, notes = _knowledge(tmp_path)
    sentiment = SentimentStore(tmp_path / "ops" / "sentiment.db")
    try:
        async with history.turn() as snapshot:
            queries = HistoryQueries(snapshot)
            rows = queries.recent(chat_id=CHAT, limit=50)
            contacts = [
                str(
                    queries.principal_contact(
                        f"49151000000{index}@s.whatsapp.net",
                        (BASE + 7 * 86_400) * 1000,
                    )
                )
                for index in range(1, 8)
            ]
        ids = {str(row["native_message_id"]): str(row["message_id"]) for row in rows}
        for index in range(7):
            notes.store_note(
                PersonNoteSeed(
                    channel="whatsapp",
                    chat_id=CHAT,
                    person_id=contacts[index],
                    input_hash=f"h{index}",
                    input_cut_ms=BASE * 1000,
                    lines=(
                        PersonNoteLine(
                            kind="stance",
                            text=f"Person {index + 1}: hostile",
                            author=f"Person {index + 1}",
                        ),
                    ),
                    sources=(("message", ids[f"m{index}"], (BASE + index * 86_400) * 1000),),
                    family="stance",
                )
            )

        class Facade:
            def stance_notes(self, **kwargs: Any):
                kwargs["family"] = "stance"
                return notes.notes(**kwargs)

        reader = StanceNotesReader(
            knowledge=Facade(),
            tuning=lambda channel, chat: True,
            bounds=StanceBounds(),
        )
        async with history.turn() as snapshot:
            only_one = reader.block_for_principals(
                "whatsapp",
                CHAT,
                [f"{PHONE}@s.whatsapp.net"],
                now_ms=(BASE + 7 * 86_400) * 1000,
                snapshot=snapshot,
            )
            everyone = reader.block_for_principals(
                "whatsapp",
                CHAT,
                [f"49151000000{index}@s.whatsapp.net" for index in range(1, 8)],
                now_ms=(BASE + 7 * 86_400) * 1000,
                snapshot=snapshot,
            )
        assert "Person 1:" in only_one and "Person 2:" not in only_one
        assert sum(f"Person {index}:" in everyone for index in range(1, 8)) == 6
        assert (
            StanceNotesReader(
                knowledge=Facade(),
                tuning=lambda channel, chat: False,
            ).block_for_principals("whatsapp", CHAT, [PHONE], now_ms=BASE * 1000)
            == ""
        )
    finally:
        sentiment.close()
        knowledge.close()
        history.close()
