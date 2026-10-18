"""Read-only before/after outcomes for recorded Participation messages."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from yeoman_gateway.history.live import HistoryBoundary, HistoryPaused
from yeoman_gateway.history.queries import HistoryQueries
from yeoman_gateway.history.reader import HistoryReader

_ACTIONS = {"silence", "react", "comment"}
_ERRORS = {
    "timeout", "provider_error", "empty_response", "invalid_response", "untrusted_intent",
    "unknown_emoji", "unknown_evidence", "context_too_large", "missing_target",
    "source_unavailable", "source_expired", "source_not_authorized",
}
_OTHER = {"direct_superseded", "deadline_expired"}


def _ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{path.expanduser().resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _generation(path: Path) -> int:
    conn = _ro(path)
    try:
        row = conn.execute("SELECT state_json FROM projector_state WHERE file='@runtime'").fetchone()
    finally:
        conn.close()
    state = json.loads(row[0]) if row else None
    if not isinstance(state, dict) or not isinstance(state.get("generation"), int):
        raise HistoryPaused("reader_unavailable")
    return state["generation"]


def _ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _open_window(switch: datetime, days: int, now: datetime) -> tuple[int, int, int, bool]:
    pivot = _ms(switch)
    start = pivot - days * 86_400_000
    requested_end = pivot + days * 86_400_000
    end = min(requested_end, _ms(now))
    return start, pivot, end, end < requested_end


def _judge_mix(
    speakups: Path, start: int, pivot: int, end: int, chats: set[str] | None
) -> dict[str, Any]:
    conn = _ro(speakups)
    try:
        rows = conn.execute(
            "SELECT channel, chat_id, created_at_ms, outcome, detail_code FROM judge_attempts "
            "WHERE channel='whatsapp' AND chat_id LIKE '%@g.us' "
            "AND created_at_ms>=? AND created_at_ms<? ORDER BY created_at_ms",
            (start, end),
        ).fetchall()
    finally:
        conn.close()
    def fresh():
        return {"actions": Counter(), "errors": Counter(), "other": Counter()}

    result: dict[str, Any] = {"before": fresh(), "after": fresh()}
    per_chat: dict[str, dict[str, dict[str, Counter[str]]]] = {}
    for row in rows:
        chat_id = str(row["chat_id"])
        if chats is not None and chat_id not in chats:
            continue
        period = "before" if int(row["created_at_ms"]) < pivot else "after"
        outcome = str(row["outcome"] or "")
        category = "actions" if outcome in _ACTIONS else "errors" if outcome in _ERRORS else (
            "other" if outcome in _OTHER else None
        )
        if category:
            result[period][category][outcome] += 1
        else:
            result[period]["other"]["unclassified"] += 1
        chat_counts = per_chat.setdefault(chat_id, {"before": fresh(), "after": fresh()})
        if category:
            chat_counts[period][category][outcome] += 1
        else:
            chat_counts[period]["other"]["unclassified"] += 1
        detail = str(row["detail_code"] or "")
        if category != "errors" and detail in _ERRORS:
            result[period]["errors"][detail] += 1
            chat_counts[period]["errors"][detail] += 1
    result = {key: {name: dict(sorted(counts.items())) for name, counts in value.items()}
              for key, value in result.items()}
    result["chats"] = {
        chat: {p: {name: dict(sorted(values.items())) for name, values in counts[p].items()}
               for p in ("before", "after")}
        for chat, counts in sorted(per_chat.items())
    }
    return result


def evaluate(
    *, history_db: Path, processing_db: Path, speakups_db: Path, switch: datetime,
    days: int, lull_minutes: int, chats: set[str] | None = None, now: datetime | None = None,
) -> dict[str, Any]:
    """Summarize chat responses to Participation text messages; retain IDs and aggregates only."""
    from datetime import UTC

    if switch.tzinfo is None or switch.utcoffset() is None:
        raise ValueError("switch must include an ISO timezone")
    if days < 1 or lull_minutes < 1:
        raise ValueError("days and lull_minutes must be positive")
    now = now or datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must include a timezone")
    if _ms(switch) > _ms(now):
        raise ValueError("switch must not be in the future")
    start, pivot, end, partial = _open_window(switch, days, now)

    process = _ro(processing_db)
    try:
        effects = process.execute(
            "SELECT DISTINCT r.channel,r.chat_id,r.provider_message_id "
            "FROM effects e JOIN transport_receipts r ON r.effect_id=e.effect_id "
            "WHERE e.origin='participation' AND e.payload_kind='text' AND e.state='sent' "
            "AND r.provider_message_id IS NOT NULL ORDER BY e.effect_id"
        ).fetchall()
    finally:
        process.close()

    reader = HistoryReader(history_db)
    generation = _generation(history_db)
    snapshot = reader.open_snapshot(HistoryBoundary(generation, ()))
    try:
        queries = HistoryQueries(snapshot)
        assistant_contacts = {str(row["contact_id"]) for row in queries._rows(
            "SELECT contact_id FROM contacts WHERE role='assistant'"
        )}
        anchors: list[dict[str, Any]] = []
        for effect in effects:
            if chats is not None and effect["chat_id"] not in chats:
                continue
            if effect["channel"] != "whatsapp" or not str(effect["chat_id"]).endswith("@g.us"):
                continue
            row = queries.native_message(
                chat_id=str(effect["chat_id"]), native_id=str(effect["provider_message_id"])
            )
            if row is None or row["direction"] != "out" or row["sent_ms"] is None:
                continue
            # A receipt is the effect anchor; history time determines its outcome window.
            if not start <= int(row["sent_ms"]) < end:
                continue
            anchors.append(row)

        outcomes: dict[str, dict[str, int]] = {"before": Counter(), "after": Counter()}
        by_chat: dict[str, dict[str, Counter[str]]] = {}
        lull_ms = lull_minutes * 60_000
        for anchor in anchors:
            anchor_ms = int(anchor["sent_ms"])
            period = "before" if anchor_ms < pivot else "after"
            chat_id = str(anchor["chat_id"])
            counters = by_chat.setdefault(chat_id, {"before": Counter(), "after": Counter()})
            # Ignore this bot message and later bot messages. Incoming messages and
            # reactions by other participants are the outcomes being measured.
            observed_until = min(anchor_ms + lull_ms, _ms(now))
            later = queries._rows(
                "SELECT message_id,sender_identifier,sender_contact_id,direction,sent_ms,reply_to_native_id "
                "FROM messages_current WHERE channel='whatsapp' AND chat_id=? AND deleted=0 "
                "AND sent_ms>? AND sent_ms<=? ORDER BY sent_ms,message_id",
                (chat_id, anchor_ms, observed_until),
            )
            reaction_rows = queries._rows(
                "SELECT actor_identifier,actor_contact_id,actor_basis,occurred_ms,payload_json FROM message_events "
                "WHERE channel='whatsapp' AND chat_id=? AND kind='reaction' "
                "AND target_message_id=? AND occurred_ms>? AND occurred_ms<=?",
                (chat_id, anchor["message_id"], anchor_ms, observed_until),
            )
            responses = [r for r in later if r["direction"] == "in"
                         and r["sender_identifier"] != anchor["sender_identifier"]
                         and r["sender_contact_id"] not in assistant_contacts]
            reactions = [r for r in reaction_rows if r["actor_basis"] != "reaction_echo"
                         and r["actor_identifier"] != anchor["sender_identifier"]
                         and r["actor_identifier"] is not None
                         and r["actor_contact_id"] not in assistant_contacts
                         and not json.loads(str(r["payload_json"])).get("removed")
                         and json.loads(str(r["payload_json"])).get("emoji")]
            # Each anchor contributes at most one to each outcome measure.
            has_reply = any(str(r["reply_to_native_id"] or "") == str(anchor["native_message_id"] or "") for r in responses)
            metrics = {
                "with_reply": has_reply,
                "with_reaction": bool(reactions),
                "with_further_messages": bool(responses),
            }
            metrics["any"] = any(metrics.values())
            for metric, happened in metrics.items():
                outcomes[period][metric] += int(happened)
                counters[period][metric] += int(happened)
            counters[period]["bot_messages"] += 1
            counters[period]["censored"] += int(observed_until < anchor_ms + lull_ms)
        # Message/event counts are raw; per-bot-message shares use the episode totals.
        totals = {p: dict(v) for p, v in outcomes.items()}
        for p in totals:
            count = sum(c[p]["bot_messages"] for c in by_chat.values())
            censored = sum(c[p]["censored"] for c in by_chat.values())
            totals[p]["bot_messages"] = count
            totals[p]["censored"] = censored
            totals[p]["observed_bot_messages"] = count - censored
            for metric in ("with_reply", "with_reaction", "with_further_messages", "any"):
                totals[p].setdefault(metric, 0)
                totals[p][f"{metric}_share"] = (
                    round(totals[p].get(metric, 0) / count, 4) if count else 0.0
                )
        for periods in by_chat.values():
            for value in periods.values():
                value.setdefault("bot_messages", 0)
                value.setdefault("censored", 0)
                denominator = value["bot_messages"]
                for metric in ("with_reply", "with_reaction", "with_further_messages", "any"):
                    value.setdefault(metric, 0)
                    value[f"{metric}_share"] = (
                        round(value[metric] / denominator, 4) if denominator else 0.0
                    )
        chats_out = {
            chat_id: {period: dict(counts[period]) for period in ("before", "after")}
            for chat_id, counts in sorted(by_chat.items())
        }
    finally:
        snapshot.close()
        reader.close()
    return {
        "switch": switch.isoformat(), "days": days, "lull_minutes": lull_minutes,
        "window_ms": {"before": [start, pivot], "after": [pivot, end]},
        "partial": partial, "coverage_end_ms": end,
        "totals": totals, "chats": chats_out,
        "judge_mix": _judge_mix(speakups_db, start, pivot, end, chats),
    }


def render_markdown(summary: dict[str, Any]) -> str:
    lines = ["# Participation outcomes", "", f"Switch: {summary['switch']}", "",
             "| period | bot messages | reply | reaction | further messages | any |",
             "|---|---:|---:|---:|---:|---:|"]
    for period in ("before", "after"):
        row = summary["totals"][period]
        lines.append(
            f"| {period} | {row.get('bot_messages', 0)} | "
            f"{row.get('with_reply', 0)} ({row.get('with_reply_share', 0):.0%}) | "
            f"{row.get('with_reaction', 0)} ({row.get('with_reaction_share', 0):.0%}) | "
            f"{row.get('with_further_messages', 0)} ({row.get('with_further_messages_share', 0):.0%}) | "
            f"{row.get('any', 0)} ({row.get('any_share', 0):.0%}) |"
        )
    if summary["chats"]:
        lines.extend(["", "| chat id | period | bot messages | reply | reaction | further messages | any |",
                      "|---|---|---:|---:|---:|---:|---:|"])
        for chat_id, periods in summary["chats"].items():
            for period in ("before", "after"):
                row = periods[period]
                lines.append(
                    f"| {chat_id} | {period} | {row.get('bot_messages', 0)} | "
                    f"{row.get('with_reply', 0)} ({row.get('with_reply_share', 0):.0%}) | "
                    f"{row.get('with_reaction', 0)} ({row.get('with_reaction_share', 0):.0%}) | "
                    f"{row.get('with_further_messages', 0)} ({row.get('with_further_messages_share', 0):.0%}) | "
                    f"{row.get('any', 0)} ({row.get('any_share', 0):.0%}) |"
                )
    lines.extend(["", "Shares are observed shares; censored lull windows are listed in the JSON.",
                  "Judge action `silence` is the recorded action for skip.",
                  "", "Judge mix (actions / errors):"])
    for period in ("before", "after"):
        mix = summary["judge_mix"][period]
        actions = ", ".join(f"{k} {v}" for k, v in mix["actions"].items()) or "none"
        errors = ", ".join(f"{k} {v}" for k, v in mix["errors"].items()) or "none"
        lines.append(f"{period}: actions {actions}; errors {errors}")
    if summary["partial"]:
        lines.extend(["", "After window is partial through the current time."])
    lines.extend(["", "No message text is included."])
    return "\n".join(lines) + "\n"


def write_outputs(directory: Path, summary: dict[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    directory.chmod(0o700)
    (directory / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (directory / "summary.md").write_text(render_markdown(summary), encoding="utf-8")
    (directory / "summary.json").chmod(0o600)
    (directory / "summary.md").chmod(0o600)
