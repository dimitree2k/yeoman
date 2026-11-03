"""Deterministic per-person stance notes for reception-tuned groups.

The builder reads only stored reception labels and the history message a label points
at.  It never reads message text, never calls a model, and never rewrites an old note:
an unchanged input hash is not stored again, and a changed one supersedes the previous
active stance note of the same person.

The notes are a separate ``stance`` family of the N1 person-note store.  N1's profile
notes keep their own supersession chain and their own reads.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from typing import Any

from loguru import logger

from yeoman_gateway.knowledge.models import PersonNoteLine, PersonNoteSeed, PersonNoteView
from yeoman_gateway.processing.person_notes_prompt import clean_text
from yeoman_gateway.reception.store import LabelRow, SentimentStore
from yeoman_gateway.reception.store import now_ms as _now_ms

STANCE_FAMILY = "stance"
DAY_MS = 86_400_000
#: The reader's independent TTL: a note whose newest evidence is older does not render.
MAX_NOTE_AGE_MS = 30 * DAY_MS
#: Longest block we ever put in a prompt; the judge/writer budgets still decide.
DEFAULT_BLOCK_MAX_CHARS = 1_500
STANCE_BLOCK_HEADER = (
    "[How people here treat Arvid · derived from past reactions, untrusted, never fact, "
    "never quote or mention it]"
)
STANCE_INSTRUCTION = (
    "Context for choosing whether and how to answer, not a rule. Someone often hostile "
    "tends not to want replies; keeping it short usually lands better than arguing on."
)
#: The same ordering the label-agreement report uses: strongest response, then confidence.
_RESPONDS_RANK = {"yes": 2, "unclear": 1, "no": 0}
_NEGATIVE = frozenset({"annoyed", "hostile"})


@dataclass(frozen=True, slots=True)
class StanceBounds:
    """Every deterministic stance knob, read from the shared config once."""

    window_days: int = 30
    min_replies: int = 3
    min_days: int = 2
    hostile_min: int = 3
    annoyed_min: int = 3
    playful_min: int = 3
    positive_min: int = 3
    signal_min: int = 3
    ratio_threshold: float = 0.5
    max_people: int = 6
    block_max_chars: int = DEFAULT_BLOCK_MAX_CHARS

    @classmethod
    def from_config(cls, config: Any) -> "StanceBounds":
        reception = config.processing.reception
        return cls(
            window_days=int(reception.stance_window_days),
            min_replies=int(reception.stance_min_replies),
            min_days=int(reception.stance_min_days),
            hostile_min=int(reception.stance_hostile_min),
            annoyed_min=int(reception.stance_annoyed_min),
            playful_min=int(reception.stance_playful_min),
            positive_min=int(reception.stance_positive_min),
            signal_min=int(reception.stance_signal_min),
            ratio_threshold=float(reception.stance_ratio_threshold),
            max_people=int(reception.stance_max_people),
            block_max_chars=int(reception.stance_block_max_chars),
        )


@dataclass(slots=True)
class StanceCounts:
    """One person's counts in one chat window; no message text, no model prose."""

    person_id: str
    person_name: str
    replies: int = 0
    hostile: int = 0
    annoyed: int = 0
    playful: int = 0
    positive: int = 0
    neutral: int = 0
    signals: dict[str, int] = field(default_factory=dict)
    days: set[str] = field(default_factory=set)
    first_ms: int = 0
    last_ms: int = 0
    source_ids: set[str] = field(default_factory=set)
    source_ms: dict[str, int] = field(default_factory=dict)

    def add(self, label: LabelRow, *, sent_ms: int, day: str) -> None:
        self.replies += 1
        if label.stance in ("hostile", "annoyed", "playful", "positive", "neutral"):
            setattr(self, label.stance, getattr(self, label.stance) + 1)
        for signal in label.signals:
            self.signals[signal] = self.signals.get(signal, 0) + 1
        self.days.add(day)
        self.first_ms = sent_ms if not self.first_ms else min(self.first_ms, sent_ms)
        self.last_ms = max(self.last_ms, sent_ms)
        self.source_ids.add(label.message_id)
        self.source_ms[label.message_id] = sent_ms

    @property
    def negative(self) -> int:
        return self.hostile + self.annoyed

    def hash_payload(self) -> dict[str, Any]:
        return {
            "person": self.person_id,
            "replies": self.replies,
            "hostile": self.hostile,
            "annoyed": self.annoyed,
            "playful": self.playful,
            "positive": self.positive,
            "neutral": self.neutral,
            "signals": dict(sorted(self.signals.items())),
            "days": sorted(self.days),
            "first_ms": self.first_ms,
            "last_ms": self.last_ms,
            "sources": sorted(self.source_ids),
        }


@dataclass(frozen=True, slots=True)
class StanceBuildReport:
    built: int = 0
    skipped_minimum: int = 0
    unchanged: int = 0
    unresolved: int = 0
    people: int = 0


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _label_rank(item: LabelRow) -> tuple[int, float]:
    return _RESPONDS_RANK.get(item.responds_to_arvid, -1), float(item.confidence)


def dedupe_reply_labels(labels: Iterable[LabelRow]) -> dict[str, LabelRow]:
    """Exactly one label per reply message: strongest response, then confidence."""
    chosen: dict[str, LabelRow] = {}
    for item in labels:
        current = chosen.get(item.message_id)
        if current is None or _label_rank(item) > _label_rank(current):
            chosen[item.message_id] = item
    return chosen


def _ratio(count: int, total: int) -> float:
    return float(count) / float(total) if total else 0.0


def build_patterns(counts: StanceCounts, bounds: StanceBounds) -> tuple[str, ...]:
    """At most two short English pattern lines, negative patterns first."""
    total = counts.replies
    if total < max(1, bounds.min_replies) or len(counts.days) < max(1, bounds.min_days):
        return ()
    patterns: list[str] = []
    ratio = bounds.ratio_threshold
    if counts.hostile >= bounds.hostile_min and _ratio(counts.hostile, total) >= ratio:
        patterns.append(
            f"often hostile toward Arvid ({counts.hostile} of {total} replies)"
        )
    elif counts.annoyed >= bounds.annoyed_min and _ratio(counts.annoyed, total) >= ratio:
        patterns.append(f"often annoyed by Arvid ({counts.annoyed} of {total})")
    if (
        len(patterns) < 2
        and counts.playful >= bounds.playful_min
        and (_ratio(counts.playful, total) >= ratio)
    ):
        patterns.append(f"teases him playfully ({counts.playful} of {total})")
    if len(patterns) < 2 and counts.signals.get("task_request", 0) >= bounds.signal_min:
        patterns.append(f"asks him for things ({counts.signals['task_request']})")
    if len(patterns) < 2 and counts.signals.get("uses_his_answer", 0) >= bounds.signal_min:
        patterns.append(f"uses his answers ({counts.signals['uses_his_answer']})")
    if (
        len(patterns) < 2
        and counts.positive >= bounds.positive_min
        and (_ratio(counts.positive, total) >= ratio)
    ):
        patterns.append(f"positive toward Arvid ({counts.positive} of {total})")
    return tuple(patterns[:2])


def render_stance_block(lines: Iterable[str], *, max_chars: int = DEFAULT_BLOCK_MAX_CHARS) -> str:
    """The one fixed block, or ``""`` when it is empty or does not fit whole."""
    body = [str(line).strip() for line in lines if str(line).strip()]
    if not body:
        return ""
    block = "\n".join([STANCE_BLOCK_HEADER, *body, STANCE_INSTRUCTION])
    if len(block) > max(0, int(max_chars)):
        return ""
    return block


def newest_evidence_ms(view: PersonNoteView) -> int:
    """Newest message timestamp recorded by a stance note (0 when none)."""
    return max(
        (int(revision) for kind, _ref, revision in view.sources if kind == "message"),
        default=0,
    )


def note_is_renderable(
    view: PersonNoteView, *, now_ms: int, max_age_ms: int = MAX_NOTE_AGE_MS
) -> bool:
    """Active stance note whose newest evidence is inside the TTL."""
    if view.family != STANCE_FAMILY or view.status != "active" or not view.lines:
        return False
    newest = newest_evidence_ms(view)
    if newest <= 0:
        return False
    return int(now_ms) - newest <= int(max_age_ms)


def _safe_person_name(value: str | None, *, limit: int = 80) -> str:
    name = clean_text(value, limit)
    if not name or not any(char.isalpha() for char in name):
        return ""
    return name


def _local_day(moment_ms: int, tz: tzinfo | None) -> str:
    moment = datetime.fromtimestamp(int(moment_ms) / 1000, tz)
    return (moment if tz is not None else moment.astimezone()).date().isoformat()


def _contact_for(queries: Any, row: Mapping[str, Any]) -> str | None:
    contact = row.get("sender_contact_id")
    if not contact:
        return None
    try:
        return queries.terminal(str(contact)) or str(contact)
    except Exception:  # pragma: no cover - defensive on a broken snapshot
        return str(contact)


def _history_name(queries: Any, person_id: str) -> str:
    row = queries.contact(person_id)
    if row is None:
        return ""
    return _safe_person_name(str(row["display_name"] or ""))


def _group_context(queries: Any, knowledge: Any, chat_id: str, now_ms: int) -> Any:
    """The same group read context N1 builds for its display-name lookup."""
    try:
        audience = queries.members(chat_id=str(chat_id), at_ms=int(now_ms))
        if audience.status != "known" or not audience.members:
            return None
        from yeoman_gateway.knowledge.read_context import build_group_read_context

        principal = sorted(audience.members)[0]
        return build_group_read_context(
            queries=queries,
            channel="whatsapp",
            chat_id=str(chat_id),
            principal=principal,
            is_owner=False,
            at_ms=int(now_ms),
            policy_revision=int(getattr(knowledge, "policy_revision", 0) or 0),
            purpose="reply",
        )
    except Exception:  # pragma: no cover - display names are advisory
        return None


class ReceptionStanceBuilder:
    """Read labels in a history turn, count per person, store deterministic notes."""

    def __init__(
        self,
        *,
        store: SentimentStore,
        history: Any,
        knowledge: Any | None = None,
        note_store: Any | None = None,
        bounds: StanceBounds | Callable[[], StanceBounds] | None = None,
        tz: tzinfo | None = None,
    ) -> None:
        self._store = store
        self._history = history
        self._knowledge = knowledge
        #: The N1 store directly, for tests and for a composition without the facade.
        self._note_store = note_store
        self._bounds = bounds or StanceBounds()
        self._tz = tz

    @property
    def tz(self) -> tzinfo | None:
        return self._tz

    def bounds(self) -> StanceBounds:
        return self._bounds() if callable(self._bounds) else self._bounds

    async def build(self, chat_id: str, *, now_ms: int | None = None) -> StanceBuildReport:
        moment = int(now_ms if now_ms is not None else _now_ms())
        if self._knowledge is None and self._note_store is None:
            return StanceBuildReport()
        bounds = self.bounds()
        since_ms = moment - max(1, bounds.window_days) * DAY_MS
        labels = [
            item
            for item in self._store.labels(chat_id=str(chat_id))
            if int(item.labeled_at_ms) >= since_ms
        ]
        replies = dedupe_reply_labels(item for item in labels if item.responds_to_arvid == "yes")
        if not replies:
            return StanceBuildReport()

        from yeoman_gateway.history.context import history_turn
        from yeoman_gateway.history.queries import HistoryQueries

        try:
            async with history_turn(self._history) as snapshot:
                queries = HistoryQueries(snapshot)
                counts, unresolved = self._count(queries, replies, now_ms=moment, bounds=bounds)
                names = self._names(queries, counts, chat_id=chat_id, now_ms=moment)
        except Exception as exc:  # noqa: BLE001 - the nightly job must not crash
            logger.warning(
                "reception_stance_read_failed chat={} error_type={}",
                str(chat_id)[:12],
                type(exc).__name__,
            )
            return StanceBuildReport(unresolved=len(replies))

        built = unchanged = skipped = 0
        for person_id, item in counts.items():
            name = names.get(person_id, "")
            if not name:
                unresolved += item.replies
                continue
            item.person_name = name
            patterns = build_patterns(item, bounds)
            if not patterns:
                skipped += 1
                continue
            payload = item.hash_payload()
            input_hash = _hash(payload)
            if self._store_has_hash(chat_id, person_id, input_hash):
                unchanged += 1
                continue
            lines = tuple(
                PersonNoteLine(
                    kind="stance",
                    text=f"{name}: {pattern}",
                    author=name,
                    refs=tuple(sorted(item.source_ids)),
                )
                for pattern in patterns
            )
            sources = tuple(
                ("message", message_id, int(item.source_ms.get(message_id, 0)))
                for message_id in sorted(item.source_ids)
            )
            self._store_note(
                chat_id=chat_id,
                person_id=person_id,
                input_hash=input_hash,
                input_cut_ms=moment,
                lines=lines,
                sources=sources,
            )
            built += 1
        logger.info(
            "reception_stance chat={} built={} unchanged={} skipped={} unresolved={}",
            str(chat_id)[:12],
            built,
            unchanged,
            skipped,
            unresolved,
        )
        return StanceBuildReport(
            built=built,
            skipped_minimum=skipped,
            unchanged=unchanged,
            unresolved=unresolved,
            people=len(counts),
        )

    def _count(
        self,
        queries: Any,
        replies: Mapping[str, LabelRow],
        *,
        now_ms: int,
        bounds: StanceBounds,
    ) -> tuple[dict[str, StanceCounts], int]:
        counts: dict[str, StanceCounts] = {}
        unresolved = 0
        for message_id, label in replies.items():
            row = queries.message(str(message_id))
            if row is None or str(row.get("direction") or "") == "out":
                unresolved += 1
                continue
            person_id = _contact_for(queries, row)
            if not person_id:
                unresolved += 1
                continue
            sent_ms = int(row.get("sent_ms") or 0)
            if sent_ms and not (now_ms - max(1, bounds.window_days) * DAY_MS <= sent_ms <= now_ms):
                continue
            item = counts.get(person_id)
            if item is None:
                item = counts[person_id] = StanceCounts(person_id=person_id, person_name="")
            item.add(label, sent_ms=sent_ms, day=_local_day(sent_ms or now_ms, self._tz))
        return counts, unresolved

    def _names(
        self,
        queries: Any,
        counts: Mapping[str, StanceCounts],
        *,
        chat_id: str,
        now_ms: int,
    ) -> dict[str, str]:
        context = (
            _group_context(queries, self._knowledge, chat_id, now_ms)
            if self._knowledge is not None
            else None
        )
        names: dict[str, str] = {}
        for person_id in counts:
            name = ""
            if self._knowledge is not None:
                try:
                    name = _safe_person_name(
                        self._knowledge.display_name(person_id, context=context, for_group=True)
                    )
                except Exception:  # pragma: no cover - names are advisory
                    name = ""
            names[person_id] = name or _history_name(queries, person_id)
        return names

    def _store_has_hash(self, chat_id: str, person_id: str, input_hash: str) -> bool:
        if self._note_store is not None:
            return bool(
                self._note_store.has_hash(
                    channel="whatsapp",
                    chat_id=str(chat_id),
                    person_id=str(person_id),
                    input_hash=input_hash,
                    family=STANCE_FAMILY,
                )
            )
        if self._knowledge is not None and hasattr(self._knowledge, "has_person_note_hash"):
            return bool(
                self._knowledge.has_person_note_hash(
                    channel="whatsapp",
                    chat_id=str(chat_id),
                    person_id=str(person_id),
                    input_hash=input_hash,
                    family=STANCE_FAMILY,
                )
            )
        return False

    def _store_note(
        self,
        *,
        chat_id: str,
        person_id: str,
        input_hash: str,
        input_cut_ms: int,
        lines: tuple[PersonNoteLine, ...],
        sources: tuple[tuple[str, str, int], ...],
    ) -> None:
        seed = PersonNoteSeed(
            channel="whatsapp",
            chat_id=str(chat_id),
            person_id=str(person_id),
            input_hash=input_hash,
            input_cut_ms=int(input_cut_ms),
            lines=lines,
            sources=sources,
            family=STANCE_FAMILY,
        )
        if self._note_store is not None:
            self._note_store.store_note(seed)
            return
        if self._knowledge is not None and hasattr(self._knowledge, "store_person_note"):
            self._knowledge.store_person_note(seed)


class ReceptionStanceJob:
    """Nightly tick for tuned chats, right after the labeler tick."""

    def __init__(
        self,
        *,
        builder: ReceptionStanceBuilder,
        chats: Callable[[], Iterable[str]],
        clock_ms: Callable[[], int] | None = None,
        run_hour: int = 3,
    ) -> None:
        self._builder = builder
        self._chats = chats
        self._clock_ms = clock_ms or _now_ms
        self._run_hour = int(run_hour)
        self._last_day: str | None = None

    async def tick(self, *, now_ms: int | None = None) -> int:
        moment = int(now_ms if now_ms is not None else self._clock_ms())
        local = datetime.fromtimestamp(moment / 1000, self._builder.tz)
        local = local if self._builder.tz is not None else local.astimezone()
        if local.hour < self._run_hour:
            return 0
        day = local.date().isoformat()
        if self._last_day == day:
            return 0
        built = 0
        for chat_id in dict.fromkeys(str(item) for item in self._chats() if str(item)):
            report = await self._builder.build(chat_id, now_ms=moment)
            built += report.built
        self._last_day = day
        return built


__all__ = [
    "DEFAULT_BLOCK_MAX_CHARS",
    "MAX_NOTE_AGE_MS",
    "STANCE_BLOCK_HEADER",
    "STANCE_FAMILY",
    "STANCE_INSTRUCTION",
    "ReceptionStanceBuilder",
    "ReceptionStanceJob",
    "StanceNotesReader",
    "StanceBounds",
    "StanceBuildReport",
    "StanceCounts",
    "build_patterns",
    "dedupe_reply_labels",
    "metadata_principals",
    "newest_evidence_ms",
    "note_is_renderable",
    "render_stance_block",
]


class StanceNotesReader:
    """Read stance notes for the people actually present in one decision.

    The reader is gated by the live policy callable, resolves principals through the
    same temporal history lookup the rest of the runtime uses, keeps the exact chat
    scope, and never widens to another chat or a direct conversation.
    """

    def __init__(
        self,
        *,
        knowledge: Any,
        tuning: Callable[[str, str], bool],
        bounds: StanceBounds | Callable[[], StanceBounds] | None = None,
    ) -> None:
        self._knowledge = knowledge
        self._tuning = tuning
        self._bounds = bounds or StanceBounds()

    def bounds(self) -> StanceBounds:
        return self._bounds() if callable(self._bounds) else self._bounds

    def enabled(self, channel: str, chat_id: str) -> bool:
        if str(channel) != "whatsapp" or not str(chat_id).endswith("@g.us"):
            return False
        try:
            return bool(self._tuning(str(channel), str(chat_id)))
        except Exception:  # noqa: BLE001 - a tuning read never breaks a reply
            return False

    def person_ids(
        self, principals: Iterable[str], *, chat_id: str, now_ms: int, snapshot: Any
    ) -> tuple[str, ...]:
        from yeoman_gateway.history.queries import HistoryQueries

        queries = HistoryQueries(snapshot)
        found: list[str] = []
        for value in principals:
            principal = str(value or "").strip()
            if not principal:
                continue
            person: str | None = None
            try:
                person = queries.principal_contact(principal, int(now_ms))
            except Exception:  # pragma: no cover - defensive
                person = None
            if not person:
                try:
                    person = queries.terminal(principal)
                except Exception:  # pragma: no cover - defensive
                    person = None
            if person and person not in found:
                found.append(str(person))
        return tuple(found)

    def block_for_principals(
        self,
        channel: str,
        chat_id: str,
        principals: Iterable[str],
        *,
        now_ms: int,
        snapshot: Any | None = None,
    ) -> str:
        """One fixed block for the current window, or ``""`` when nothing renders."""
        if self._knowledge is None or not self.enabled(channel, chat_id):
            return ""
        if snapshot is None:
            from yeoman_gateway.history.context import current_history_snapshot

            snapshot = current_history_snapshot()
        if snapshot is None:
            return ""
        from yeoman_gateway.history.queries import HistoryQueries

        queries = HistoryQueries(snapshot)
        person_ids = self.person_ids(principals, chat_id=chat_id, now_ms=now_ms, snapshot=snapshot)
        if not person_ids:
            return ""
        try:
            views = self._knowledge.stance_notes(
                channel=channel,
                chat_id=chat_id,
                person_ids=person_ids,
                source_present=lambda message_id: queries.message(message_id) is not None,
                limit=max(1, self.bounds().max_people * 4),
            )
        except Exception as exc:  # noqa: BLE001 - notes are advisory
            logger.warning(
                "reception_stance_read_failed chat={} error_type={}",
                str(chat_id)[:12],
                type(exc).__name__,
            )
            return ""
        chosen: list[str] = []
        seen: set[str] = set()
        for view in views:
            if view.person_id in seen or len(seen) >= self.bounds().max_people:
                continue
            if not note_is_renderable(view, now_ms=now_ms):
                continue
            seen.add(view.person_id)
            chosen.extend(line.text for line in view.lines if line.kind == "stance")
        return render_stance_block(chosen, max_chars=self.bounds().block_max_chars)


def metadata_principals(metadata: Mapping[str, Any] | None) -> tuple[str, ...]:
    """Sender principals visible in the direct-reply context windows."""
    if not isinstance(metadata, Mapping):
        return ()
    found: list[str] = []

    def _add(value: Any) -> None:
        text = str(value or "").strip()
        if text and text not in found:
            found.append(text)

    _add(metadata.get("sender_id"))
    for key in ("ambient_context_window", "reply_context_window"):
        rows = metadata.get(key)
        if not isinstance(rows, (list, tuple)):
            continue
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            _add(row.get("sender_id") or row.get("participant") or row.get("sender"))
    return tuple(found)
