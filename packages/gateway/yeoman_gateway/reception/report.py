"""Owner reporting that reads ``sentiment.db`` only (P3)."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Any

from yeoman_gateway.history.group_signals import previous_week, week_of
from yeoman_gateway.reception.store import LabelRow, SentimentStore, WeeklyRow

TREND_UP = "↑"
TREND_DOWN = "↓"
TREND_FLAT = "→"
UNKNOWN = "?"


def _percent(value: Any) -> str:
    try:
        return f"{float(value) * 100:.0f}%"
    except (TypeError, ValueError):
        return UNKNOWN


def _number(value: Any, digits: int = 1) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return UNKNOWN


def _persona(row: WeeklyRow) -> str:
    name = row.persona_file.rsplit("/", 1)[-1] if row.persona_file else ""
    return name or (row.persona_sha[:8] if row.persona_sha else UNKNOWN)


def _metric(row: WeeklyRow, key: str, default: Any = 0) -> Any:
    return row.metrics.get(key, default)


def trend_arrow(current: WeeklyRow, previous: WeeklyRow | None) -> str:
    """Direct-response share against the previous stored week."""
    if previous is None or previous.status != "ok" or current.status != "ok":
        return TREND_FLAT
    try:
        now = float(_metric(current, "direct_rate"))
        before = float(_metric(previous, "direct_rate"))
    except (TypeError, ValueError):
        return TREND_FLAT
    if now > before + 0.02:
        return TREND_UP
    if now < before - 0.02:
        return TREND_DOWN
    return TREND_FLAT


def _stance_distribution(labels: Sequence[LabelRow]) -> str:
    counts = Counter(item.stance for item in labels)
    if not counts:
        return UNKNOWN
    order = ("positive", "playful", "neutral", "annoyed", "hostile")
    return " ".join(f"{key[:3]} {counts[key]}" for key in order if counts[key])


def _asks(labels: Sequence[LabelRow]) -> int:
    return sum(1 for item in labels if "asks_to_remove_or_mute" in item.signals)


def _row_line(
    row: WeeklyRow, labels: Sequence[LabelRow], previous: WeeklyRow | None
) -> str:
    if row.status != "ok":
        return (
            f"{row.iso_week} | zu wenig Daten "
            f"({_metric(row, 'arvid_messages')} Arvid / "
            f"{_metric(row, 'human_messages')} human)"
        )
    return (
        f"{row.iso_week} | {_persona(row)} | "
        f"Arvid {_metric(row, 'arvid_messages')} "
        f"({_metric(row, 'addressed')} addressed / {_metric(row, 'unprompted')} unprompted) | "
        f"direct {_percent(_metric(row, 'direct_rate'))} | "
        f"indirect {_percent(_metric(row, 'indirect_rate'))} | "
        f"silence {_percent(_metric(row, 'silence_rate'))} | "
        f"reactions {_number(_metric(row, 'reactions_per_arvid'), 2)}/Arvid vs "
        f"{_number(_metric(row, 'reactions_per_human'), 2)}/human | "
        f"negative {_metric(row, 'negative_reactions')} | "
        f"breadth {_metric(row, 'breadth')} (top {_percent(_metric(row, 'top_person_share'))}) | "
        f"stance {_stance_distribution(labels)} | "
        f"remove/mute {_asks(labels)} | trend {trend_arrow(row, previous)}"
    )


def render_report(
    store: SentimentStore,
    *,
    chat_labels: Mapping[str, str] | None = None,
    chat_ids: Iterable[str] | None = None,
    weeks: int = 8,
    tz: tzinfo | None = None,
) -> str:
    """One text table per chat; insufficient weeks say ``zu wenig Daten``."""
    labels_by_chat = chat_labels or {}
    ids = list(dict.fromkeys(chat_ids or (row.chat_id for row in store.weekly())))
    if not ids:
        return "Keine Empfangsdaten gespeichert."
    blocks: list[str] = []
    for chat_id in ids:
        rows = list(store.weekly(chat_id, weeks=weeks))
        label = labels_by_chat.get(chat_id, chat_id)
        if not rows:
            blocks.append(f"{label}\n  keine Daten")
            continue
        blocks.append(f"{label}")
        for index, row in enumerate(rows):
            previous = rows[index + 1] if index + 1 < len(rows) else None
            labels = store.labels(chat_id=chat_id, iso_week=row.iso_week)
            blocks.append(f"  {_row_line(row, labels, previous)}")
    return "\n".join(blocks)


def resolve_chat_selector(
    selector: str, *, chat_comments: Mapping[str, str], chat_ids: Iterable[str]
) -> str | None:
    """Accept a chat id or the human ``comment`` name from policy."""
    value = str(selector or "").strip()
    if not value:
        return None
    known = list(dict.fromkeys(str(item) for item in chat_ids if str(item)))
    if value in known:
        return value
    by_comment = {comment.casefold(): chat_id for chat_id, comment in chat_comments.items()}
    return by_comment.get(value.casefold())


def persona_before_after(
    store: SentimentStore, *, chat_id: str, now_ms: int
) -> str | None:
    """First digest 7 days after a persona change: compare week before vs week after."""
    changes = store.persona_changes(chat_id)
    if not changes:
        return None
    sha, changed_ms = changes[-1]
    if int(now_ms) - int(changed_ms) < 7 * 24 * 60 * 60 * 1000:
        return None
    if not store.persona_report_pending(chat_id=chat_id, persona_sha=sha):
        return None
    after_week = week_of(changed_ms + 7 * 24 * 60 * 60 * 1000).label
    rows = {row.iso_week: row for row in store.weekly(chat_id)}
    after = rows.get(after_week)
    before = rows.get(previous_week(changed_ms).label)
    if before is None or after is None or before.status != "ok" or after.status != "ok":
        return None
    return (
        f"Persona {sha[:8]}: direct {_percent(_metric(before, 'direct_rate'))} → "
        f"{_percent(_metric(after, 'direct_rate'))}, silence "
        f"{_percent(_metric(before, 'silence_rate'))} → "
        f"{_percent(_metric(after, 'silence_rate'))} (7 Tage davor/danach)"
    )


@dataclass(frozen=True, slots=True)
class DigestLine:
    chat_id: str
    label: str
    text: str


def weekly_digest(
    store: SentimentStore,
    *,
    chat_labels: Mapping[str, str],
    chat_ids: Iterable[str],
    now_ms: int,
    quotes: Mapping[str, Sequence[str]] | None = None,
) -> tuple[str, tuple[str, ...]]:
    """One compact owner message; quotes are passed in from live history only."""
    lines: list[str] = []
    used: list[str] = []
    spoken = quotes or {}
    for chat_id in dict.fromkeys(str(item) for item in chat_ids if str(item)):
        rows = store.weekly(chat_id, weeks=2)
        label = chat_labels.get(chat_id, chat_id)
        if not rows:
            lines.append(f"{label}: zu wenig Daten")
            continue
        row = rows[0]
        if row.status != "ok":
            lines.append(f"{label}: zu wenig Daten")
            continue
        previous = rows[1] if len(rows) > 1 else None
        engaged = 1.0 - float(_metric(row, "silence_rate"))
        lines.append(
            f"{label}: Arvid {_metric(row, 'arvid_messages')} · "
            f"engaged {_percent(engaged)} · silence "
            f"{_percent(_metric(row, 'silence_rate'))} · "
            f"stance {_stance_distribution(store.labels(chat_id=chat_id, iso_week=row.iso_week))} "
            f"· {trend_arrow(row, previous)}"
        )
        for quote in (spoken.get(chat_id) or ())[:3]:
            clean = " ".join(str(quote).split())[:120]
            if clean:
                lines.append(f"  „{clean}“")
                used.append(chat_id)
        before_after = persona_before_after(store, chat_id=chat_id, now_ms=now_ms)
        if before_after is not None:
            lines.append(f"  {before_after}")
            store.mark_persona_reported(
                chat_id=chat_id,
                persona_sha=store.persona_changes(chat_id)[-1][0],
                now_ms=now_ms,
            )
    if not lines:
        return "", tuple()
    return "Reception Wochenbericht\n" + "\n".join(lines), tuple(dict.fromkeys(used))


_RESPONDS_RANK = {"yes": 2, "unclear": 1, "no": 0}
_NEGATIVE_STANCES = frozenset({"annoyed", "hostile"})


def _label_rank(item: LabelRow) -> tuple[int, float]:
    return _RESPONDS_RANK.get(item.responds_to_arvid, -1), float(item.confidence)


def label_agreement(
    *,
    expected: Sequence[Mapping[str, str]],
    actual: Sequence[LabelRow],
) -> dict[str, Any]:
    """Accuracy and confusion counts for a CSV of ``message_id,expected_*`` rows.

    A reply can carry one label per Arvid message it followed; it is judged as a
    whole: the strongest ``responds_to_arvid`` wins, and its most confident label
    gives the stance.
    """
    by_message: dict[str, LabelRow] = {}
    for item in actual:
        current = by_message.get(item.message_id)
        if current is None or _label_rank(item) > _label_rank(current):
            by_message[item.message_id] = item
    responds_total = responds_match = stance_total = stance_match = 0
    negative_match = 0
    responds_confusion: Counter[tuple[str, str]] = Counter()
    stance_confusion: Counter[tuple[str, str]] = Counter()
    missing = 0
    seen = set()
    for expected_row in expected:
        message_id = str(expected_row.get("message_id") or "")
        if not message_id or message_id in seen:
            continue
        seen.add(message_id)
        want_responds = str(expected_row.get("expected_responds") or "").strip()
        want_stance = str(expected_row.get("expected_stance") or "").strip()
        found = by_message.get(message_id)
        if found is None:
            # Unlabelled replies (media, outside the window) are counted, not scored.
            missing += 1
            continue
        found_responds = found.responds_to_arvid
        found_stance = found.stance
        if want_responds:
            responds_total += 1
            responds_confusion[(want_responds, found_responds)] += 1
            responds_match += int(want_responds == found_responds)
        if want_stance:
            stance_total += 1
            stance_confusion[(want_stance, found_stance)] += 1
            stance_match += int(want_stance == found_stance)
            negative_match += int(
                (want_stance in _NEGATIVE_STANCES) == (found_stance in _NEGATIVE_STANCES)
            )
    return {
        "rows": len(seen),
        "missing_labels": missing,
        "responds_total": responds_total,
        "responds_accuracy": round(responds_match / responds_total, 4)
        if responds_total else 0.0,
        "stance_total": stance_total,
        "stance_accuracy": round(stance_match / stance_total, 4) if stance_total else 0.0,
        "negative_accuracy": round(negative_match / stance_total, 4)
        if stance_total else 0.0,
        "responds_confusion": {
            f"{want}->{got}": count for (want, got), count in sorted(responds_confusion.items())
        },
        "stance_confusion": {
            f"{want}->{got}": count for (want, got), count in sorted(stance_confusion.items())
        },
    }


def read_expected_csv(path: Any) -> list[dict[str, str]]:
    """Read the coordinator's CSV without inventing any text field."""
    import csv

    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        rows: list[dict[str, str]] = []
        for raw in reader:
            rows.append({
                "message_id": str(raw.get("message_id") or "").strip(),
                "expected_responds": str(raw.get("expected_responds") or "").strip(),
                "expected_stance": str(raw.get("expected_stance") or "").strip(),
            })
    return rows


def local_day(moment_ms: int, tz: tzinfo | None = None) -> str:
    moment = datetime.fromtimestamp(int(moment_ms) / 1000, tz)
    return (moment if tz is not None else moment.astimezone()).date().isoformat()


__all__ = [
    "DigestLine",
    "TREND_DOWN",
    "TREND_FLAT",
    "TREND_UP",
    "label_agreement",
    "persona_before_after",
    "read_expected_csv",
    "render_report",
    "resolve_chat_selector",
    "trend_arrow",
    "weekly_digest",
]
