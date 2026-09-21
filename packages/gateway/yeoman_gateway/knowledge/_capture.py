"""Forward statement capture: proven observed sources become sourced statements.

Three decisions stay separate here, exactly as the spec requires (section 1.4 and the
owner clarification of 2026-09-20):

* **Observation** is the journal's job.  Every event the channel accepts is committed to
  ``ProcessingStore.events`` before anything downstream looks at it - with or without a
  response turn, reply decision, audience proof or promotion switch.
* **Authorized processing** needs a proven audience.  A source whose audience cannot be
  proven is refused for promotion and keeps its observation; it is never downgraded to a
  guess and never silently dropped.
* **Promotion** is what this module assembles: a durable forward boundary, bounded source
  batches, one idempotent job per batch, and statements that reference the sources they
  rest on.

Nothing here reads a response turn to decide eligibility.  Turn links stay optional
context.  No new store, queue framework or provider is introduced: jobs live in the
existing ``knowledge_jobs`` table and statements are published through the existing
``KnowledgeService.capture``.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from yeoman_gateway.knowledge.models import (
    SourceRef,
    TrustedCaptureContext,
)

#: How long a chat must be quiet before its source batch is promoted.  The same shape as
#: the shared-fact queue: an idle gap keeps one statement from being split in half by the
#: next message, a hard delay keeps a busy chat from postponing promotion forever.
DEFAULT_IDLE_MS = 60_000
DEFAULT_MAX_DELAY_MS = 300_000
#: Bounded queue: an overflowing batch is recorded as ``skipped``, never lost.
DEFAULT_MAX_WAITING = 64
#: Hard cap per batch, so one job cannot grow without bound.
DEFAULT_BATCH_MAX = 8
DEFAULT_WINDOW_LIMIT = 200

#: Boundary keys in ``knowledge_meta``.  The pair (commit time, event id) is the durable
#: forward cursor: a restart resumes *after* it instead of re-promoting history.
BOUNDARY_MS_KEY = "statement_capture_boundary_ms"
BOUNDARY_EVENT_KEY = "statement_capture_boundary_event_id"
BOUNDARY_STARTED_KEY = "statement_capture_started_ms"

#: Audiences that count as proven.  ``unknown`` fails closed for processing.
PROVEN_AUDIENCES = ("known", "author_only")
#: Payload roles that are never a human statement, however they were journaled.
NON_HUMAN_ROLES = ("assistant", "system", "bot", "tool")


@dataclass(frozen=True, slots=True)
class ObservedEvent:
    """One durably journaled message with the identity and proof it really has.

    The identity is the journal's, not a caller's: event id, revision, channel, chat,
    author principal and the *event's* timestamp.  The authority projection adds the
    audience status and the revocation state; both come from the store, never from a
    model or a caller argument.
    """

    event_id: str
    revision: int
    channel: str
    chat_id: str
    principal: str
    occurred_ms: int
    created_ms: int
    text: str
    kind: str = "message"
    direction: str = "in"
    origin: str = ""
    provider_message_id: str = ""
    role: str = "user"
    audience_status: str = "unknown"
    audience_members: tuple[str, ...] = ()
    revoked: bool = False

    @property
    def provider_key(self) -> tuple[str, str, str]:
        """The physical provider message this event describes."""
        return (self.channel, self.chat_id, self.provider_message_id)

    @property
    def source(self) -> SourceRef:
        return SourceRef(
            event_id=self.event_id,
            revision=self.revision,
            channel=self.channel,
            chat_id=self.chat_id,
            author_principal=self.principal,
            occurred_at_ms=self.occurred_ms,
        )

    @property
    def is_group(self) -> bool:
        return self.chat_id.endswith("@g.us")


def observed_event(event: Any, authority: Mapping[str, Any] | None = None) -> ObservedEvent:
    """Project one journal event plus its authority row into a promotable view."""
    payload = getattr(event, "payload", None)
    body: Mapping[str, Any] = payload if isinstance(payload, Mapping) else {}
    entry: Mapping[str, Any] = authority if isinstance(authority, Mapping) else {}
    members = entry.get("audience_members")
    return ObservedEvent(
        event_id=str(getattr(event, "event_id", "") or ""),
        revision=int(getattr(event, "revision", 1) or 1),
        channel=str(getattr(event, "channel", "") or ""),
        chat_id=str(getattr(event, "chat_id", "") or ""),
        principal=str(getattr(event, "principal", "") or entry.get("author_principal") or ""),
        occurred_ms=int(
            getattr(event, "occurred_ms", 0)
            or entry.get("occurred_at_ms")
            or getattr(event, "created_ms", 0)
            or 0
        ),
        created_ms=int(getattr(event, "created_ms", 0) or 0),
        text=str(body.get("text") or "").strip(),
        kind=str(getattr(event, "kind", "") or ""),
        direction=str(getattr(event, "direction", "") or ""),
        origin=str(getattr(event, "origin", "") or ""),
        provider_message_id=str(getattr(event, "source_message_id", "") or ""),
        role=str(body.get("role") or "user").strip().lower() or "user",
        audience_status=str(entry.get("audience_status") or "unknown"),
        audience_members=tuple(str(item) for item in (members or ())),
        revoked=entry.get("revoked_at_ms") is not None,
    )


def promoter_reason(item: ObservedEvent) -> str:
    """Why one observed event may not become a statement source (``""`` = eligible).

    The refusal is a *promotion* decision only.  The observation stays in the journal and
    keeps its own disclosure rules; refusing here removes nothing.
    """
    if item.kind != "message":
        return "not_a_message"
    if item.direction and item.direction != "in":
        return "not_inbound"
    if item.role in NON_HUMAN_ROLES:
        return "not_human_source"
    if not item.principal:
        return "missing_author"
    if not item.text:
        return "empty_text"
    if item.audience_status not in PROVEN_AUDIENCES:
        return "unknown_audience"
    if item.audience_status == "known" and not item.audience_members:
        return "unknown_audience"
    if item.revoked:
        return "source_revoked"
    return ""


def collapse_provider_duplicates(items: Iterable[ObservedEvent]) -> list[ObservedEvent]:
    """One source per physical provider message, preferring the Bridge identity.

    A message can be journaled under two identities (Bridge signal sink and policy gate).
    Both rows stay durable evidence, but a statement must not rest on "two" sources that
    are really one message: duplicates collapse to a single source, and the canonical
    Bridge row wins because it is the one the Bridge ACKed.
    """
    collapsed: dict[tuple[str, str, str], ObservedEvent] = {}
    order: list[tuple[str, str, str]] = []
    for item in items:
        key = item.provider_key
        if not item.provider_message_id:
            key = (item.channel, item.chat_id, f"event:{item.event_id}")
        current = collapsed.get(key)
        if current is None:
            collapsed[key] = item
            order.append(key)
            continue
        if _prefer(item, current):
            collapsed[key] = item
    return [collapsed[key] for key in order]


def _prefer(candidate: ObservedEvent, current: ObservedEvent) -> bool:
    """Which of two journal rows for one physical message is the better source."""
    from yeoman_gateway.processing.models import CANONICAL_WHATSAPP_ORIGIN

    candidate_canonical = candidate.origin == CANONICAL_WHATSAPP_ORIGIN
    current_canonical = current.origin == CANONICAL_WHATSAPP_ORIGIN
    if candidate_canonical != current_canonical:
        return candidate_canonical
    if candidate.created_ms != current.created_ms:
        return candidate.created_ms < current.created_ms
    return candidate.event_id < current.event_id


@dataclass(slots=True)
class SourceBatch:
    """One chat's forward source window and whether it may be promoted yet."""

    channel: str
    chat_id: str
    sources: list[ObservedEvent] = field(default_factory=list)
    indexes: list[int] = field(default_factory=list)
    refused: dict[str, int] = field(default_factory=dict)

    @property
    def scope_key(self) -> str:
        return f"channel:{self.channel}:chat:{self.chat_id}"

    def refusal(self, reason: str) -> None:
        if reason:
            self.refused[reason] = self.refused.get(reason, 0) + 1

    def reset(self) -> None:
        """Close the batch: the promoted or refused events are accounted for."""
        self.sources = []
        self.indexes = []
        self.refused = {}


class ObservedSourceRegistrar:
    """Registers the audience proof of an observed event - turn or no turn.

    The audience comes from the live chat registry (groups) or from the author alone
    (direct chats).  Nothing is registered when neither is provable: the observation
    stays durable with an unknown audience, and promotion refuses it later instead of
    inventing a reader list.
    """

    def __init__(
        self,
        *,
        knowledge: Any,
        processing: Any | None = None,
        chat_registry: Any | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._knowledge = knowledge
        self._processing = processing
        self._chat_registry = chat_registry
        self._clock = clock or (lambda: int(time.time() * 1000))

    def __call__(self, event_id: str) -> bool:
        return self.register(event_id)

    def register(self, event_id: str) -> bool:
        register = getattr(self._knowledge, "register_turn_source", None)
        get_event = getattr(self._processing, "get_event", None)
        if register is None or get_event is None or not str(event_id or "").strip():
            return False
        event = get_event(str(event_id))
        if event is None:
            return False
        if str(getattr(event, "kind", "") or "") != "message":
            return False
        direction = str(getattr(event, "direction", "") or "")
        if direction and direction != "in":
            return False
        channel = str(getattr(event, "channel", "") or "")
        chat_id = str(getattr(event, "chat_id", "") or "")
        principal = str(getattr(event, "principal", "") or "")
        if not channel or not chat_id or not principal:
            return False
        source = observed_event(event).source
        if not chat_id.endswith("@g.us"):
            # A direct conversation has exactly one proven reader: its author.
            return bool(
                register(
                    source=source,
                    verified_members=frozenset({principal}),
                    snapshot_id=f"{channel}:{chat_id}:author",
                    author_only=True,
                )
            )
        members = self._proven_members(channel, chat_id)
        if not members:
            return False
        return bool(
            register(
                source=source,
                verified_members=frozenset(members),
                snapshot_id=f"{channel}:{chat_id}:{len(members)}",
            )
        )

    def _proven_members(self, channel: str, chat_id: str) -> frozenset[str]:
        if self._chat_registry is None:
            return frozenset()
        from yeoman_gateway.knowledge._memory.read_gate import registry_members

        proven = registry_members(self._chat_registry, channel=channel, chat_id=chat_id)
        return frozenset(str(item) for item in proven if item)


@dataclass(slots=True)
class AudienceRepairReport:
    """Counted outcome of one historic audience-repair pass."""

    examined: int = 0
    registered: int = 0
    created: int = 0
    already_proven: int = 0
    refused: dict[str, int] = field(default_factory=dict)
    dry_run: bool = True

    def refuse(self, reason: str) -> None:
        if reason:
            self.refused[reason] = self.refused.get(reason, 0) + 1


class HistoricAudienceRepair:
    """Registers a *provable* audience for revisions that were journaled without one.

    Historic rows carry no source-time membership snapshot.  The registry knows today's
    members, not who was in the group when the message was written, so registering the
    current list would hand a later member a right that was never proven.  A historic
    revision therefore becomes ``author_only``: the author is provable, the reader list is
    not.  The pass is bounded, idempotent and refuses revoked revisions.
    """

    def __init__(
        self, *, knowledge: Any, processing: Any, clock: Callable[[], int] | None = None
    ) -> None:
        self._knowledge = knowledge
        self._processing = processing
        self._clock = clock or (lambda: int(time.time() * 1000))

    def run(self, *, limit: int = 500, apply: bool = False) -> AudienceRepairReport:
        report = AudienceRepairReport(dry_run=not apply)
        reader = getattr(self._processing, "unproven_event_sources", None)
        if not callable(reader):
            report.refuse("store_cannot_list_unproven_sources")
            return report
        rows = reader(limit=max(1, int(limit)))
        register = getattr(self._knowledge, "register_turn_source", None)
        get_authority = getattr(self._processing, "get_event_source_authority", None)
        for row in rows:
            report.examined += 1
            if str(row.get("direction") or "in") != "in":
                report.refuse("not_inbound")
                continue
            if row.get("revoked_at_ms") is not None:
                report.refuse("source_revoked")
                continue
            principal = str(row.get("author_principal") or "")
            if not principal:
                report.refuse("missing_author")
                continue
            if str(row.get("audience_status") or "unknown") != "unknown":
                report.already_proven += 1
                continue
            if not callable(register):
                report.refuse("knowledge_cannot_register")
                continue
            entry: Mapping[str, Any] | None = None
            if callable(get_authority):
                entry = get_authority(str(row["event_id"]), int(row.get("revision") or 1))
            if entry is None:
                # No projection row yet: the second journal writer predates it.  The
                # registration below creates the row from the event's own provenance.
                report.created += 1
            if not apply:
                report.registered += 1
                continue
            source = observed_event(_RowEvent(row, entry or {}), entry or {}).source
            try:
                registered = register(
                    source=source,
                    verified_members=frozenset({principal}),
                    snapshot_id=f"historic:{source.channel}:{source.chat_id}",
                    author_only=True,
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "historic audience repair failed event_id={} error_type={}",
                    row.get("event_id"),
                    type(exc).__name__,
                )
                report.refuse("registration_failed")
                continue
            if registered:
                report.registered += 1
            else:
                report.refuse("registration_refused")
        return report


class _RowEvent:
    """The journal fields an authority row already carries, without reading the event."""

    def __init__(self, row: Mapping[str, Any], entry: Mapping[str, Any]) -> None:
        self.event_id = str(row.get("event_id") or "")
        self.revision = int(row.get("revision") or 1)
        self.channel = str(row.get("source_channel") or "")
        self.chat_id = str(row.get("source_chat_id") or "")
        self.principal = str(row.get("author_principal") or "")
        self.occurred_ms = int(row.get("occurred_at_ms") or 0)
        self.created_ms = int(row.get("created_ms") or entry.get("created_ms") or 0)
        self.kind = str(row.get("kind") or "message")
        self.direction = str(row.get("direction") or "in")
        self.origin = str(row.get("origin") or "")
        self.source_message_id = ""
        self.payload: Mapping[str, Any] = {}


@dataclass(slots=True)
class CaptureReport:
    """Counted outcome of one promotion pass. Never contains statement content."""

    examined: int = 0
    promoted_sources: int = 0
    jobs: int = 0
    already_queued: int = 0
    refusals: dict[str, int] = field(default_factory=dict)

    def refuse(self, reason: str) -> None:
        if reason:
            self.refusals[reason] = self.refusals.get(reason, 0) + 1

    @property
    def refusal_total(self) -> int:
        return sum(self.refusals.values())


class StatementCaptureProducer:
    """Turns the durable forward window into bounded, idempotent promotion jobs.

    The producer never reads a response turn.  Eligibility comes from the journal plus the
    authority projection; the batch trigger is source quiescence (idle gap, batch cap or
    hard delay), so a chat nobody ever answers still gets its observations promoted.
    """

    def __init__(
        self,
        *,
        knowledge: Any,
        processing: Any,
        idle_ms: int = DEFAULT_IDLE_MS,
        max_delay_ms: int = DEFAULT_MAX_DELAY_MS,
        batch_max: int = DEFAULT_BATCH_MAX,
        max_waiting: int = DEFAULT_MAX_WAITING,
        window_limit: int = DEFAULT_WINDOW_LIMIT,
        extractor_version: str = "statement-capture-v1",
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._knowledge = knowledge
        self._processing = processing
        self._idle_ms = max(1, int(idle_ms))
        self._max_delay_ms = max(self._idle_ms, int(max_delay_ms))
        self._batch_max = max(1, int(batch_max))
        self._max_waiting = max(1, int(max_waiting))
        self._window_limit = max(1, int(window_limit))
        self._extractor_version = str(extractor_version)
        self._clock = clock or (lambda: int(time.time() * 1000))
        self.overflows = 0

    # -- durable forward boundary ---------------------------------------------

    def boundary(self) -> tuple[int, str]:
        reader = getattr(self._knowledge, "capture_boundary", None)
        value = reader() if callable(reader) else None
        if not value:
            return (0, "")
        return (int(value[0]), str(value[1]))

    def initialize_boundary(self, *, now_ms: int | None = None) -> tuple[int, str]:
        """Start forward capture *now*: historic observations are never promoted here."""
        moment = int(now_ms if now_ms is not None else self._clock())
        setter = getattr(self._knowledge, "set_capture_boundary", None)
        if callable(setter):
            setter(moment, "")
        logger.info("statement capture boundary initialized at {}", moment)
        return (moment, "")

    # -- one promotion pass ----------------------------------------------------

    def run_due(self, *, now_ms: int | None = None) -> CaptureReport:
        moment = int(now_ms if now_ms is not None else self._clock())
        report = CaptureReport()
        start_ms, start_event_id = self.boundary()
        if not start_ms and not start_event_id:
            # No boundary yet: this is an activation, not a backfill.
            self.initialize_boundary(now_ms=moment)
            return report

        events = self._forward_window(start_ms, start_event_id)
        if not events:
            return report
        report.examined = len(events)
        truncated = len(events) >= self._window_limit

        batches = self._group(events, report=report)

        # Second pass: close every batch that is due and may be closed.
        consumed = [False] * len(events)
        for batch in batches.values():
            if not batch.sources:
                # Nothing promotable to wait for: count the refusals and move on.
                for position in batch.indexes:
                    consumed[position] = True
                continue
            if (
                truncated
                and batch.indexes[-1] == len(events) - 1
                and len(batch.sources) < self._batch_max
            ):
                # The window edge may cut this chat's batch in half; the next window
                # completes it instead of promoting a statement from half a conversation.
                continue
            if not self._batch_due(batch, now_ms=moment):
                continue
            if not self._promote(batch, report=report, now_ms=moment):
                # A full queue is visible and recoverable: leave the batch unconsumed so
                # the next pass retries the same idempotent job.
                continue
            for position in batch.indexes:
                consumed[position] = True

        prefix_end = -1
        for index, done in enumerate(consumed):
            if not done:
                break
            prefix_end = index
        if prefix_end >= 0:
            last = events[prefix_end]
            self._advance_boundary(
                int(getattr(last, "created_ms", 0) or 0),
                str(getattr(last, "event_id", "") or ""),
            )
        return report

    def _group(
        self, events: Sequence[Any], *, report: CaptureReport
    ) -> dict[tuple[str, str], SourceBatch]:
        """Group a window per chat.

        A batch is decided once, after the whole window is known: deciding per event would
        promote the first message of a conversation before its neighbour was even read.
        Refusals are counted here and never remove the observation.
        """
        batches: dict[tuple[str, str], SourceBatch] = {}
        for index, event in enumerate(events):
            item = observed_event(event, self._authority_for(event))
            key = (item.channel, item.chat_id)
            batch = batches.get(key)
            if batch is None:
                batch = SourceBatch(channel=item.channel, chat_id=item.chat_id)
                batches[key] = batch
            batch.indexes.append(index)
            reason = promoter_reason(item)
            if reason:
                batch.refusal(reason)
                report.refuse(reason)
            else:
                batch.sources.append(item)
        return batches

    def run_historical(
        self,
        *,
        before_ms: int,
        max_batches: int = 20,
        scan_limit: int = 2000,
        apply: bool = False,
    ) -> CaptureReport:
        """Bounded promotion of a historic window.  The forward boundary never moves.

        Historic promotion is an explicit, owner-authorized operation: it is dry-run by
        default, it stops after ``max_batches`` *new* jobs, and a batch that is already
        queued is skipped instead of counted, so repeated passes walk forward instead of
        re-reporting the same work.
        """
        moment = int(self._clock())
        report = CaptureReport()
        reader = getattr(self._processing, "events_after", None)
        if not callable(reader):
            return report
        try:
            events = tuple(
                reader(after_ms=0, before_ms=int(before_ms), limit=max(1, int(scan_limit)))
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("historic capture window read failed error_type={}", type(exc).__name__)
            return report
        if not events:
            return report
        report.examined = len(events)
        ceiling = max(1, int(max_batches))
        for batch in self._group(events, report=report).values():
            if not batch.sources:
                continue
            # A historic window can hold a chat's whole history at once, so the batch cap
            # has to be enforced here exactly as the forward path enforces it: a job with
            # hundreds of sources would silently truncate the prompt and stop being a
            # bounded batch.  Sources are chunked in window order, after duplicates
            # collapse, and each chunk is one idempotent job.
            sources = collapse_provider_duplicates(batch.sources)
            for start in range(0, len(sources), self._batch_max):
                chunk = sources[start : start + self._batch_max]
                refs = tuple(item.source for item in chunk)
                if not apply:
                    report.promoted_sources += len(refs)
                    report.jobs += 1
                    if report.jobs >= ceiling:
                        return report
                    continue
                result = self._enqueue(refs, scope_key=batch.scope_key, now_ms=moment)
                state = str(getattr(result, "state", "") or "")
                reason = str(getattr(result, "reason", "") or "")
                if state == "skipped":
                    self.overflows += 1
                    report.refuse(reason or "queue_full")
                    continue
                if state not in ("queued", "running", "done"):
                    report.refuse(reason or "not_queued")
                    continue
                if reason == "already_queued":
                    report.already_queued += 1
                    continue
                report.jobs += 1
                report.promoted_sources += len(refs)
                if report.jobs >= ceiling:
                    return report
        return report

    def _forward_window(self, start_ms: int, start_event_id: str) -> tuple[Any, ...]:
        reader = getattr(self._processing, "events_after", None)
        if not callable(reader):
            return ()
        try:
            return tuple(
                reader(
                    after_ms=int(start_ms),
                    after_event_id=str(start_event_id or ""),
                    limit=self._window_limit,
                )
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("statement capture window read failed error_type={}", type(exc).__name__)
            return ()

    def _authority_for(self, event: Any) -> Mapping[str, Any] | None:
        getter = getattr(self._processing, "get_event_source_authority", None)
        if not callable(getter):
            return None
        try:
            return getter(
                str(getattr(event, "event_id", "")), int(getattr(event, "revision", 1) or 1)
            )
        except Exception:  # pragma: no cover - defensive
            return None

    def _batch_due(self, batch: SourceBatch, *, now_ms: int) -> bool:
        if not batch.sources:
            return False
        newest = max(item.created_ms for item in batch.sources)
        oldest = min(item.created_ms for item in batch.sources)
        if len(batch.sources) >= self._batch_max:
            return True
        if newest and now_ms - newest >= self._idle_ms:
            return True
        return bool(oldest) and now_ms - oldest >= self._max_delay_ms

    def _promote(self, batch: SourceBatch, *, report: CaptureReport, now_ms: int) -> bool:
        """Queue one batch.  Returns False when the queue was full and nothing was queued."""
        sources = collapse_provider_duplicates(batch.sources)
        if not sources:
            return True
        refs = tuple(item.source for item in sources)
        report.promoted_sources += len(refs)
        result = self._enqueue(refs, scope_key=batch.scope_key, now_ms=now_ms)
        state = str(getattr(result, "state", "") or "")
        if state in ("queued", "running", "done"):
            report.jobs += 1
            return True
        if state == "skipped":
            self.overflows += 1
            report.refuse(str(getattr(result, "reason", "") or "queue_full"))
        return False

    def _enqueue(self, refs: tuple[SourceRef, ...], *, scope_key: str, now_ms: int) -> Any:
        enqueue = getattr(self._knowledge, "enqueue_capture", None)
        if not callable(enqueue):
            return None
        context = TrustedCaptureContext(
            request_id=f"capture:{scope_key}:{now_ms}",
            policy_revision=self._policy_revision(),
            capture_basis="observed_source_batch",
            authorized_sources=refs,
        )
        try:
            return enqueue(
                refs,
                context=context,
                scope_key=scope_key,
                extractor_version=self._extractor_version,
                max_waiting=self._max_waiting,
                due_ms=int(now_ms),
                ts_ms=int(now_ms),
            )
        except Exception as exc:
            logger.warning("statement capture enqueue failed error_type={}", type(exc).__name__)
            return None

    def _policy_revision(self) -> int:
        value = getattr(self._knowledge, "policy_revision", 1)
        return int(value if isinstance(value, int) else 1)

    def _advance_boundary(self, created_ms: int, event_id: str) -> None:
        if not created_ms:
            return
        setter = getattr(self._knowledge, "set_capture_boundary", None)
        if callable(setter):
            setter(int(created_ms), str(event_id))


def source_texts(items: Sequence[ObservedEvent]) -> tuple[str, ...]:
    """The literal source texts an extraction may see, in stable order."""
    return tuple(item.text for item in items)
