from __future__ import annotations

import csv
import json
import os
import sqlite3
from pathlib import Path

from yeoman_gateway.processing.models import ReactionPayload
from yeoman_gateway.processing.store import ProcessingStore
from yeoman_gateway.short_reply.evaluation import (
    chat_hash,
    derive_bot_participants,
    load_replay_rows,
    load_synthetic,
    read_labels,
    sample_for_labels,
    write_label_sheet,
)

CHAT = "grp@g.us"
BOT = "bot@lid"


def _archive(path: Path, rows: list[tuple]) -> Path:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE inbound_messages (channel TEXT, chat_id TEXT, message_id TEXT, "
        "participant TEXT, sender_id TEXT, text TEXT, timestamp INTEGER, created_at TEXT, "
        "sender_name TEXT, reply_to_message_id TEXT)"
    )
    con.executemany(
        "INSERT INTO inbound_messages VALUES ('whatsapp', ?, ?, ?, ?, ?, ?, '', '', ?)", rows
    )
    con.commit()
    con.close()
    return path


def _processing(path: Path) -> Path:
    store = ProcessingStore(path)
    store.enqueue_effect(
        effect_id="e-react",
        operation_key=f"reaction:whatsapp:{CHAT}:H1:👀",
        payload=ReactionPayload(message_id="H1", emoji="👀"),
        now_ms=2_000_000_000_000,
        trace_id="H1",
        capability="send_reaction",
        target={"channel": "whatsapp", "chat_id": CHAT},
        state="sent",
    )
    store.close()
    con = sqlite3.connect(path)
    con.execute(
        "INSERT INTO transport_receipts (receipt_id, effect_id, attempt_id, channel, chat_id, "
        "provider_message_id, client_message_id, confirmed_ms, detail) "
        "VALUES ('r1', 'e-react', 'a1', 'whatsapp', ?, 'B1', NULL, 1, NULL)",
        (CHAT,),
    )
    con.commit()
    con.close()
    return path


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    archive = _archive(
        tmp_path / "reply_context.db",
        [
            (CHAT, "B1", BOT, BOT, "Starker Trade.", 2_000_000_000, None),
            (CHAT, "H1", "alex@lid", "alex", "Jepp, hatte Glück 😊", 2_000_000_010, "B1"),
            (CHAT, "H2", "bob@lid", "bob", "und du?", 2_000_000_020, "H1"),
            (CHAT, "H3", "alex@lid", "alex", "Wie lief's?", 2_000_000_030, "B1"),
            (CHAT, "B2", BOT, BOT, "Meinst du?", 2_000_000_040, "H3"),
            (CHAT, "H0", "alex@lid", "alex", "ok", 1_000_000_000, "B1"),
        ],
    )
    return archive, _processing(tmp_path / "processing.db")


def test_bot_participants_are_derived_from_receipts(tmp_path: Path) -> None:
    archive, processing = _fixture(tmp_path)
    assert derive_bot_participants(archive, processing) == {BOT}


def test_replay_keeps_only_human_replies_to_arvid(tmp_path: Path) -> None:
    archive, processing = _fixture(tmp_path)
    rows = load_replay_rows(archive, processing, since_ts=0)
    by_id = {row.message_id: row for row in rows}

    assert set(by_id) == {"H0", "H1", "H3"}  # H2 replies to a human; B2 is Arvid himself
    h1 = by_id["H1"]
    assert h1.bot_text == "Starker Trade."
    assert (h1.legacy_mode, h1.legacy_emoji) == ("low_content_reply", "👀")
    assert h1.actual == "reaction:👀"
    assert h1.graphemes == 19 and h1.question is False
    assert by_id["H3"].question is True
    assert by_id["H0"].actual == "unknown"  # older than the processing history
    assert h1.chat == chat_hash(CHAT) and h1.chat != CHAT and len(h1.chat) == 64
    assert h1.row_id.startswith(f"{chat_hash(CHAT)}:") and "H1" not in h1.row_id


def test_replay_joins_media_fields_from_inbound_jsonl(tmp_path: Path) -> None:
    archive, processing = _fixture(tmp_path)
    inbound = tmp_path / "inbound.jsonl"
    inbound.write_text(
        json.dumps({"message_id": "H1", "raw_metadata": {
            "media_kind": "audio", "voice_transcript": "ありがとう！"
        }}) + "\n",
        encoding="utf-8",
    )
    rows = load_replay_rows(archive, processing, since_ts=0, inbound_jsonl=[inbound])
    h1 = next(row for row in rows if row.message_id == "H1")
    assert h1.media_kind == "audio" and h1.media_text == "ありがとう！"
    assert h1.graphemes == 6 and h1.media_metadata_available is True


def test_replay_marks_legacy_repair_precedence(tmp_path: Path) -> None:
    archive = _archive(
        tmp_path / "reply_context.db",
        [
            (CHAT, "B1", BOT, BOT, "Starker Trade.", 2_000_000_000, None),
            (CHAT, "R1", "alex@lid", "alex", "falsch", 2_000_000_010, "B1"),
        ],
    )
    rows = load_replay_rows(archive, _processing(tmp_path / "processing.db"), since_ts=0)
    assert [row.priority_mode for row in rows] == ["repair_feedback"]


def test_sticker_candidate_does_not_require_text_graphemes() -> None:
    from yeoman_gateway.short_reply.evaluation import ReplayRow, _row_is_candidate

    sticker = ReplayRow(
        row_id="sticker", chat="c", chat_id=CHAT, message_id="S1", timestamp=1,
        text="", bot_text="", graphemes=0, question=False, bot_asked=False,
        media_kind="sticker", media_text="sticker", media_metadata_available=True,
        priority_mode="", candidate=True, legacy_mode="reply_to_bot",
        legacy_emoji="", actual="none",
    )
    assert _row_is_candidate(sticker, max_chars=80)


def test_sampling_is_deterministic_and_keeps_every_bot_question_reply(tmp_path: Path) -> None:
    archive, processing = _fixture(tmp_path)
    rows = load_replay_rows(archive, processing, since_ts=0)
    first = sample_for_labels(rows, size=2)
    assert first == sample_for_labels(rows, size=2)
    assert all(row in first for row in rows if row.bot_asked)


def test_the_label_sheet_hides_chat_ids_and_survives_a_spreadsheet(tmp_path: Path) -> None:
    archive, processing = _fixture(tmp_path)
    rows = load_replay_rows(archive, processing, since_ts=0)
    sheet = tmp_path / "labels.csv"
    write_label_sheet(rows, sheet)
    assert CHAT not in sheet.read_text(encoding="utf-8")
    assert "H1" not in sheet.read_text(encoding="utf-8")

    with sheet.open(encoding="utf-8", newline="") as handle:
        table = list(csv.DictReader(handle))
    for record in table:
        record["label_action"] = " react "
        record["label_emojis"] = "😎  😄 "
    table.append({key: "" for key in table[0]})
    with sheet.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(table[0]))
        writer.writeheader()
        writer.writerows(table)

    os.chmod(sheet, 0o644)  # simulate a spreadsheet replacing it with default permissions
    labels = read_labels(sheet)
    assert (sheet.stat().st_mode & 0o777) == 0o600
    assert (sheet.parent.stat().st_mode & 0o777) == 0o700
    assert len(labels) == len(rows)
    assert next(iter(labels.values())).action == "react"
    assert next(iter(labels.values())).emojis == ("😎", "😄")
    assert (sheet.stat().st_mode & 0o777) == 0o600
    assert (sheet.parent.stat().st_mode & 0o777) == 0o700


def test_sequence_replay_ignores_sent_effects_without_receipts(tmp_path: Path) -> None:
    from yeoman_gateway.short_reply.evaluation import load_effect_sequences

    processing_db = _processing(tmp_path / "processing.db")
    store = ProcessingStore(processing_db)
    store.enqueue_effect(
        effect_id="e-unconfirmed",
        operation_key=f"reaction:whatsapp:{CHAT}:H2:😂",
        payload=ReactionPayload(message_id="H2", emoji="😂"),
        now_ms=2_000_000_000_001,
        capability="send_reaction",
        target={"channel": "whatsapp", "chat_id": CHAT},
        state="sent",
    )
    store.close()
    reactions, _texts = load_effect_sequences(processing_db, since_ms=0)
    assert reactions == [(CHAT, 2_000_000_000, "👀")]


def test_synthetic_rows_are_marked_and_labelled(tmp_path: Path) -> None:
    path = tmp_path / "synthetic.csv"
    path.write_text(
        "lang,bot_text,text,label_action,label_emojis\n"
        "ja,良い取引でした。,ありがとう！,react,🙏 🤙\n",
        encoding="utf-8",
    )
    rows, labels = load_synthetic(path)
    assert (path.stat().st_mode & 0o777) == 0o600
    assert (path.parent.stat().st_mode & 0o777) == 0o700
    assert rows[0].synthetic is True and rows[0].chat == "synthetic-ja"
    assert labels[rows[0].row_id].emojis == ("🙏", "🤙")
    assert json.dumps(rows[0].text, ensure_ascii=False) == '"ありがとう！"'
