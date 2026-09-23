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


async def test_simulation_varies_faces_per_chat_and_skips_bot_questions() -> None:
    from yeoman_gateway.short_reply.decider import ShortReplyVerdict
    from yeoman_gateway.short_reply.evaluation import ReplayRow, simulate_decisions
    from yeoman_shared.config.schema import ProcessingShortReplyConfig

    class _Decider:
        def __init__(self) -> None:
            self.calls = 0

        async def decide(self, request):
            self.calls += 1
            return ShortReplyVerdict(
                action="react", emojis=("😂", "💀", "😄"),
                usage={"prompt_tokens": 100, "completion_tokens": 10}, latency_ms=7,
            )

    def row(
        index: int, text: str, bot_text: str = "Starker Trade.", priority_mode: str = ""
    ) -> ReplayRow:
        return ReplayRow(
            row_id=f"c:{index}", chat="c", chat_id="c@g.us", message_id=str(index),
            timestamp=1_000 + index * 600, text=text, bot_text=bot_text, graphemes=len(text),
            question=False, bot_asked=bot_text.endswith("?"), legacy_mode="low_content_reply",
            media_kind="", media_text="", media_metadata_available=True,
            priority_mode=priority_mode, candidate=True,
            legacy_emoji="👀", actual="none",
        )

    decider = _Decider()
    settings = ProcessingShortReplyConfig.model_validate(
        {"mode": "live", "rateLimit": {"count": 10}}
    )
    decisions = await simulate_decisions(
        [row(1, "lol"), row(2, "haha"), row(3, "ok", bot_text="Wirklich?"), row(4, "xD")],
        decider,
        settings,
    )
    assert [d.chosen for d in decisions] == ["😂", "💀", "", "😄"]
    assert decisions[2].kind == "answer" and decisions[2].source == "rule"
    assert decider.calls == 3
    assert decisions[0].prompt_tokens == 100 and decisions[0].model_latency_ms == 7

    protected = row(5, "Danke", priority_mode="repair_feedback")
    skipped = await simulate_decisions([protected], decider, settings)
    assert skipped[0].reason == "legacy_precedence" and not skipped[0].model_called
    assert decider.calls == 3


def _rows_and_labels():
    from yeoman_gateway.short_reply.evaluation import Label, ReplayRow, SimDecision

    def row(index, text, graphemes, legacy_emoji="👀", question=False):
        return ReplayRow(
            row_id=f"c:{index}", chat="c", chat_id="c@g.us", message_id=str(index),
            timestamp=1_000 + index * 60, text=text, bot_text="x", graphemes=graphemes,
            question=question, bot_asked=False,
            media_kind="", media_text="", media_metadata_available=True,
            priority_mode="", candidate=True,
            legacy_mode="low_content_reply" if legacy_emoji else "reply_to_bot",
            legacy_emoji=legacy_emoji,
            actual=("text" if index == 3 else f"reaction:{legacy_emoji}" if legacy_emoji else "none"),
        )

    rows = [row(1, "lol", 3), row(2, "danke", 5), row(3, "long correction", 95, ""),
            row(4, "k", 1)]
    labels = {
        "c:1": Label("react", ("😂", "💀")),
        "c:2": Label("react", ("🙏",)),
        "c:3": Label("answer", ()),
        "c:4": Label("none", ()),
    }

    def dec(row_id, kind, chosen="", emojis=()):
        return SimDecision(row_id, 1_000, kind, chosen, "model", "model", kind,
                           emojis, "", True, 100, 10, 50)

    decisions = [
        dec("c:1", "react", "💀", ("😂", "💀")),
        dec("c:2", "react", "🙏", ("🙏",)),
        dec("c:3", "answer"),
        dec("c:4", "silence"),
    ]
    return rows, labels, decisions


def test_labelled_quality_metrics_use_actual_legacy_baseline() -> None:
    from yeoman_gateway.short_reply.evaluation import evaluate, go_no_go

    rows, labels, decisions = _rows_and_labels()
    metrics = evaluate(rows, labels, decisions, max_chars=80)
    v2 = metrics["V2_varied"]
    assert v2.action_accuracy == 1.0 and v2.emoji_fit == 1.0
    assert v2.lost_answers == 0.0 and v2.unnecessary_reactions == 0.0
    v1 = metrics["V1_first_candidate"]
    assert v1.emoji_fit == 1.0  # 😂 is also in the label set
    v0 = metrics["V0_legacy"]
    assert v0.emoji_fit == 0.0  # 👀 fits nothing here
    assert v0.unnecessary_reactions == 0.25  # "k" was labelled none but got 👀
    ok, reasons = go_no_go(metrics)
    assert ok is False and any("shadow" in reason for reason in reasons)


def test_rows_above_max_chars_are_answered_in_model_variants() -> None:
    from yeoman_gateway.short_reply.evaluation import evaluate

    rows, labels, decisions = _rows_and_labels()
    tight = evaluate(rows, labels, decisions, max_chars=2)["V2_varied"]
    # "lol" (3) and "danke" (5) now exceed 2 graphemes -> normal answer path
    assert tight.unnecessary_answers == 0.5


def test_usage_summary_reports_tokens_and_only_supplied_prices() -> None:
    from yeoman_gateway.short_reply.evaluation import usage_summary

    _rows, _labels, decisions = _rows_and_labels()
    summary = usage_summary(decisions)
    assert summary["calls"] == 4 and summary["prompt_tokens_sum"] == 400
    assert "cost_usd" not in summary
    priced = usage_summary(decisions, price_in_per_mtok=1.0, price_out_per_mtok=2.0)
    assert priced["cost_usd"] == (400 * 1.0 + 40 * 2.0) / 1_000_000
    assert sum(usage_summary(decisions)["calls_per_day_europe_berlin"].values()) == 4


def test_full_sequence_metrics_group_share_by_chat_and_iso_week() -> None:
    from yeoman_gateway.short_reply.evaluation import sequence_metrics

    events = [("c@g.us", 1_790_000_000 + index, emoji) for index, emoji in enumerate(
        ["😂", "😂", "😂", "👍", "👍"]
    )]
    text_events = [("c@g.us", 1_790_000_000, "Danke 😂")]
    metrics = sequence_metrics(events, text_events)
    assert list(metrics.top_share_by_chat_week.values()) == [0.6]
    assert round(list(metrics.entropy_bits_by_chat_week.values())[0], 3) == 0.971
    assert metrics.triple_repeats == 1
    assert metrics.max_reactions_per_10min == 5
    assert metrics.max_reactions_per_hour == 5


def test_text_markers_do_not_count_and_one_emoji_per_message() -> None:
    from yeoman_gateway.short_reply.evaluation import message_emoji, sequence_metrics

    assert message_emoji("🟢 Kaufen\n🟢 Halten\n- ⚠️ Risiko") is None
    assert message_emoji("🟢 Kaufen, klar 😎") == "😎"
    assert message_emoji("Danke 😂😂😂") == "😂"
    assert message_emoji("😄") == "😄"
    metrics = sequence_metrics([], [
        ("c@g.us", 1_790_000_000, "🟢 Kaufen\n🟢 Halten\n🟢 Beobachten"),
        ("c@g.us", 1_790_000_001, "Danke 😂😂😂"),
        ("c@g.us", 1_790_000_002, "😄"),
    ])
    assert metrics.text_emoji_count == 2
    assert metrics.text_triple_repeats == 0


def test_the_same_emoji_in_three_consecutive_texts_is_a_triple() -> None:
    from yeoman_gateway.short_reply.evaluation import sequence_metrics

    metrics = sequence_metrics([], [
        ("c@g.us", 1_790_000_000 + index, text)
        for index, text in enumerate(("gut 😂", "lol 😂", "😂"))
    ])
    assert metrics.text_triple_repeats == 1


def test_history_unavailable_is_not_counted_as_a_provider_call() -> None:
    from yeoman_gateway.short_reply.evaluation import SimDecision, usage_summary

    skipped = SimDecision(
        row_id="c:1", timestamp=1_000, kind="silence", chosen="", source="none",
        reason="history_unavailable", verdict_action="-", verdict_emojis=(),
        error="history_unavailable", model_called=False, prompt_tokens=None,
        completion_tokens=None, model_latency_ms=0,
    )
    assert usage_summary([skipped])["calls"] == 0


def test_final_gate_rejects_incomplete_receipts_and_offline_fallbacks() -> None:
    from yeoman_gateway.short_reply.evaluation import (
        RolloutMetrics, evaluate, go_no_go, sequence_metrics,
    )

    rows, labels, decisions = _rows_and_labels()
    metrics = evaluate(rows, labels, decisions, max_chars=80)
    events = [
        ("c@g.us", 1_790_000_000 + index * 3_600, emoji)
        for index, emoji in enumerate(("😂", "👍", "🙏", "🤙", "😄"))
    ]
    sequence = sequence_metrics(events, [])
    rollout = RolloutMetrics(
        baseline=sequence, shadow=sequence, baseline_receipts_complete=False,
        offline_failures=1, observed_days=3, expected_candidates=5,
        logged_candidates=5, shadow_dropped=0, provider_errors=0,
        invalid_json=0, truncated=0, fallbacks=0,
    )
    probe = [decisions[0]] * 5
    ok, reasons = go_no_go(metrics, rollout=rollout, probe=probe)
    assert ok is False
    assert any("receipt coverage" in reason for reason in reasons)
    assert any("offline model errors/fallbacks" in reason for reason in reasons)
