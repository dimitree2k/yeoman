"""Deterministic reception windows (P1) on synthetic rows only."""

from __future__ import annotations

import json
from datetime import UTC

from yeoman_gateway.history.group_signals import (
    BASELINE_EVERY,
    compute_reception,
    iso_week,
)

CHAT = "120363000000000042@g.us"
MIN = 60_000
WEEK = iso_week("2026-W40", tz=UTC)
T0 = WEEK.start_ms + 9 * 60 * MIN
ARVID_ID = "15550000001@s.whatsapp.net"


def _row(
    mid: str,
    minute: float,
    sender: str = "a",
    *,
    out: bool = False,
    text: str = "synthetic text",
    reply_to: str | None = None,
    media: dict | None = None,
    mentions: list[str] | None = None,
) -> dict[str, object]:
    return {
        "message_id": f"h-{mid}",
        "native_message_id": mid,
        "chat_id": CHAT,
        "sent_ms": T0 + int(minute * MIN),
        "direction": "out" if out else "in",
        "sender_contact_id": None if out else f"c-{sender}",
        "sender_identifier": None if out else f"{sender}@s.whatsapp.net",
        "reply_to_native_id": reply_to,
        "current_text": text,
        "media_json": json.dumps(media) if media else None,
        "mentions_json": json.dumps(mentions) if mentions else None,
    }


def _reception(rows, reactions=(), **kwargs):
    return compute_reception(
        rows, reactions, chat_id=CHAT, week=WEEK,
        arvid_identifiers=(ARVID_ID,), arvid_names=("Arvid Bot",), **kwargs
    )


def test_direct_indirect_others_silence_quote_mention_name_and_sticker() -> None:
    rows = [
        # Quote-reply within 30 minutes: direct.
        _row("o1", 0, out=True),
        _row("m1", 2, "a", reply_to="o1"),
        # The addressed author writes again without quoting: indirect.
        _row("m0", 99, "a"),
        _row("o2", 100, out=True, reply_to="m0"),
        _row("m2", 102, "a"),
        # Any other human within 5 minutes: others.
        _row("o3", 200, out=True),
        _row("m3", 202, "b"),
        # No human within 10 minutes: silence.
        _row("o4", 300, out=True),
        _row("m4", 315, "a"),
        # A sticker counts as the addressed person engaging again: indirect.
        _row("m4b", 399, "b"),
        _row("o5", 400, out=True, reply_to="m4b"),
        _row("m5", 402, "b", text="", media={"kind": "sticker"}),
        # @-mention of one of his identifiers: direct.
        _row("o6", 500, out=True),
        _row("m6", 502, "c", mentions=[ARVID_ID]),
        # His display name as a whole word: direct.
        _row("o7", 600, out=True),
        _row("m7", 602, "d", text="hey Arvid, what do you think?"),
        # Arvid answered a message that was addressed to him: addressed.
        _row("m8", 699, "e", mentions=[ARVID_ID]),
        _row("o8", 700, out=True, reply_to="m8"),
    ]
    metrics = _reception(rows)
    by_id = {item.message_id: item for item in metrics.windows}
    assert (by_id["h-o1"].direct, by_id["h-o1"].category) == (True, "direct")
    assert (by_id["h-o2"].indirect, by_id["h-o2"].category) == (True, "indirect")
    assert (by_id["h-o3"].others, by_id["h-o3"].category) == (True, "others")
    assert (by_id["h-o4"].silent, by_id["h-o4"].category) == (True, "silence")
    assert (by_id["h-o5"].indirect, by_id["h-o5"].category) == (True, "indirect")
    assert (by_id["h-o6"].direct, by_id["h-o7"].direct) == (True, True)
    assert by_id["h-o8"].addressed is True
    assert metrics.addressed == 1 and metrics.unprompted == 7
    assert (metrics.direct, metrics.indirect, metrics.others, metrics.silence) == (
        3, 2, 1, 2
    )


def test_topic_change_is_not_negative_and_reactions_breadth_and_given_are_counted() -> None:
    rows = [_row("o1", 0, out=True), _row("m1", 2, "a", reply_to="o1"),
            _row("m2", 3, "b", reply_to="o1")]
    reactions = [
        {"event_id": "e1", "target_message_id": "h-o1", "actor_contact_id": "c-a",
         "emoji": "👍", "occurred_ms": T0 + MIN},
        {"event_id": "e2", "target_message_id": "h-o1", "actor_contact_id": "c-b",
         "emoji": "🙄", "occurred_ms": T0 + MIN},
        {"event_id": "e3", "target_message_id": "h-m1", "actor_contact_id": "c-a",
         "emoji": "🔥", "occurred_ms": T0 + MIN},
        {"event_id": "e4", "target_message_id": "h-m1", "actor_contact_id": "arvid-contact",
         "emoji": "😂", "occurred_ms": T0 + MIN},
    ]
    metrics = _reception(
        rows, reactions, assistant_contact_ids={"arvid-contact"},
        contact_names={"c-a": "Alpha", "c-b": "Beta"},
    )
    assert metrics.negative_reactions == 1
    assert metrics.reactions_received == 2
    window = metrics.windows[0]
    assert dict(window.emoji_counts)["👍"] == 1
    assert dict(window.emoji_counts)["🙄"] == 1
    assert metrics.human_reactions == 2
    assert metrics.arvid_reactions_given == 1
    assert metrics.engaged_people == ("c-a", "c-b")
    assert metrics.top_person_share == 0.5
    assert metrics.reactions_received / metrics.arvid_messages == 2.0
    assert "synthetic text" not in json.dumps(metrics.metrics())


def test_baseline_samples_every_kth_human_quote_reply() -> None:
    rows: list[dict[str, object]] = []
    for index in range(10):
        rows.append(_row(f"t{index}", index * 100, f"p{index % 3}"))
        rows.append(_row(f"r{index}", index * 100 + 1, "x", reply_to=f"t{index}"))
    metrics = _reception(rows, baseline_every=BASELINE_EVERY, baseline_max=2)
    assert metrics.baseline_sampled == 2
    assert len(metrics.baseline_windows) == 2
    assert metrics.baseline_silence == 2


def test_insufficient_data_needs_both_floors_below_the_minimum() -> None:
    few = [_row(f"o{i}", i * 100, out=True) for i in range(4)]
    humans = [_row(f"h{i}", 10_000 + i) for i in range(20)]
    assert _reception([*few, *humans]).status == "insufficient_data"
    enough_humans = [_row(f"h{i}", 2_000 + i * 5) for i in range(50)]
    assert _reception([*few, *enough_humans]).status == "ok"
    enough_arvid = [_row(f"o{i}", i * 100, out=True) for i in range(5)]
    assert _reception([*enough_arvid, *humans]).status == "ok"


def test_outbound_metadata_is_attached_when_available() -> None:
    rows = [_row("o1", 0, out=True)]
    meta = {"o1": {"action_type": "answer", "persona_sha": "abc123"}}
    window = _reception(rows, outbound_meta=meta).windows[0]
    assert (window.action_type, window.persona_sha) == ("answer", "abc123")
    missing = _reception(rows).windows[0]
    assert (missing.action_type, missing.persona_sha) == ("unknown", "unknown")


def test_identifier_in_plain_text_counts_as_a_direct_mention() -> None:
    rows = [
        _row("o1", 0, out=True),
        _row("m1", 2, "a", text=f"@{ARVID_ID.split('@')[0]} bitte antworten"),
    ]
    window = _reception(rows).windows[0]
    assert (window.direct, window.category) == (True, "direct")
