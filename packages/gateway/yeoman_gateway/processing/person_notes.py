"""Person notes: one model call per (group, person) and its daily job.

The deterministic half (``stated`` lines) reuses the Knowledge read gate through
``recall(..., group_wide=True)``; the model half reads only that person's own messages
in the same group, last 60 days, inside one untrusted envelope.  A note is stored only
after every observed ref resolved and every stated author resolved to a group-safe
display name.

The scheduler is small on purpose and independent of the group-state service: when
``processing.personNotes.mode`` is ``build`` the gateway starts this job, and nothing
else reads the notes yet.  ``off`` builds, queues and reads nothing.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from typing import Any

from loguru import logger

from yeoman_gateway.history.ids import PN_SUFFIX
from yeoman_gateway.knowledge.models import (
    PersonNoteLine,
    PersonNoteSeed,
    PersonNoteView,
    RecallQuery,
    TrustedReadContext,
)
from yeoman_gateway.knowledge.read_context import build_group_read_context
from yeoman_gateway.processing.model_route import RouteUnavailableError
from yeoman_gateway.processing.person_notes_prompt import (
    MAX_INPUT_CHARS,
    MAX_LINES_PER_NOTE,
    ObservedInput,
    build_observed_input,
    clean_text,
    observed_messages,
    parse_observed_lines,
)

PERSON_NOTES_ROUTE = "knowledge.notes"
MAX_ATTEMPTS = 3
RETRY_BACKOFF_MS = 15 * 60_000
STALE_RUNNING_MS = 30 * 60_000
JOBS_PER_TICK = 4
#: Own-message window of the observation.
OBSERVATION_DAYS = 60
DAY_MS = 86_400_000
#: Bounded history read: the newest messages of the person win the char cap.
MAX_OWN_MESSAGES = 300


@dataclass(frozen=True, slots=True)
class PlannedLine:
    """One deterministic stated line plus the statement revision it rests on."""

    line: PersonNoteLine
    revision: int = 0


@dataclass(frozen=True, slots=True)
class PersonNotePlan:
    """Everything one note build needs, read under one history/knowledge lease."""

    channel: str
    chat_id: str
    person_id: str
    person_name: str
    input_hash: str
    input_cut_ms: int
    stated: tuple[PlannedLine, ...]
    observed: ObservedInput
    member_names: tuple[str, ...]
    outsider_names: tuple[str, ...]
    new_messages: int = 0

    @property
    def estimated_tokens(self) -> int:
        stated = sum(len(item.line.text) // 4 + 4 for item in self.stated)
        return int(self.observed.estimated_tokens + stated)


@dataclass(frozen=True, slots=True)
class PersonNoteOutcome:
    state: str
    reason: str = ""
    note: PersonNoteView | None = None
    model_calls: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    dropped: int = 0


@dataclass(slots=True)
class PersonNoteTickReport:
    route_missing: bool = False
    enqueued: int = 0
    recovered: int = 0
    ran: int = 0
    skipped_budget: int = 0
    outcomes: dict[str, int] = field(default_factory=dict)


def _short(value: str) -> str:
    return str(value)[:12]


def _day(now_ms: int, tz: tzinfo | None) -> str:
    moment = datetime.fromtimestamp(int(now_ms) / 1000, tz)
    if tz is None:
        moment = moment.astimezone()
    return moment.date().isoformat()


def _response_format(route: Any) -> dict[str, Any] | None:
    mode = str(getattr(route, "structured_output", "json_object") or "json_object")
    return {"type": "json_object"} if mode != "prompt_only" else None


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def stated_lines(
    knowledge: Any,
    person_id: str,
    *,
    context: TrustedReadContext,
    limit: int = MAX_LINES_PER_NOTE,
) -> tuple[PlannedLine, ...]:
    """Deterministic lines from statements a group reader may see about this person.

    The gate is the ordinary ``recall(..., group_wide=True)`` path: author-only rows,
    direct-chat rows and other-chat rows never enter.  The author must resolve to a
    group-safe display name, otherwise the line is dropped rather than rendered as a
    jid or an unknown label.
    """
    recalled = knowledge.recall(
        RecallQuery(person_ids=(str(person_id),), limit=max(limit * 2, limit)),
        context=context,
        group_wide=True,
        max_chars=sys.maxsize,
    )
    rows: list[PlannedLine] = []
    for statement_id in recalled.statement_ids:
        if len(rows) >= max(0, int(limit)):
            break
        speaker, content, updated_ms = _statement_for_line(knowledge, statement_id)
        if not speaker:
            continue
        author = clean_text(knowledge.display_name(speaker, context=context, for_group=True), 60)
        if not author or not any(char.isalpha() for char in author):
            continue
        text = clean_text(content)
        if not text:
            continue
        rows.append(
            PlannedLine(
                line=PersonNoteLine(
                    kind="stated",
                    text=f"{author}: {text}",
                    author=str(author),
                    refs=(str(statement_id),),
                ),
                revision=int(updated_ms or 0),
            )
        )
    return tuple(rows)


def _statement_for_line(knowledge: Any, statement_id: str) -> tuple[str, str, int]:
    """The permitted row's speaker, text and revision.  Read after the recall gate."""
    store = getattr(knowledge, "_store", None)
    if store is None:  # pragma: no cover - only a test double without a store
        return "", "", 0
    row = store.query_one(
        "SELECT s.speaker_person_id AS speaker, s.updated_ms AS updated_ms,"
        " n.content AS content FROM knowledge_statements s"
        " JOIN memory2_nodes n ON n.id = s.statement_id WHERE s.statement_id = ?",
        (str(statement_id),),
    )
    if row is None:
        return "", "", 0
    return (
        str(row["speaker"] or ""),
        str(row["content"] or ""),
        int(row["updated_ms"] or 0),
    )


def _own_messages(
    queries: Any, *, chat_id: str, person_id: str, now_ms: int, since_ms: int | None = None
) -> tuple[list[tuple[str, str]], int]:
    """The person's own visible messages in this chat, oldest first, plus new count."""
    rows = queries.recent(
        chat_id=str(chat_id),
        limit=MAX_OWN_MESSAGES,
        after_ms=int(now_ms) - OBSERVATION_DAYS * DAY_MS,
    )
    own: list[tuple[str, str]] = []
    new = 0
    for row in rows:
        if str(row["direction"]) != "in":
            continue
        contact = row["sender_contact_id"]
        if not contact:
            continue
        try:
            terminal = queries.terminal(str(contact))
        except Exception:  # pragma: no cover - defensive on a broken snapshot
            terminal = None
        if terminal != str(person_id):
            continue
        sent = int(row["sent_ms"] or 0)
        if since_ms is not None and sent > int(since_ms):
            new += 1
        own.append((str(row["message_id"]), str(row["text"] or "")))
    return own, new


def _member_identifiers(principal: str) -> tuple[str, ...]:
    """Identifier forms a roster principal can resolve under.

    A roster principal is ``whatsapp:<number>`` while history resolves full identifiers,
    so a bare number is tried as a phone jid first and then as a LID.
    """
    value = principal.partition(":")[2] or principal
    if "@" in value:
        return (value,)
    return (value + PN_SUFFIX, value + "@lid")


def _member_contacts(queries: Any, members: Iterable[str], *, at_ms: int) -> dict[str, str]:
    """Current member contact id -> display name, through proven identifier resolution."""
    out: dict[str, str] = {}
    for principal in members:
        contact = None
        for identifier in _member_identifiers(str(principal)):
            try:
                contact = queries.resolve_identifier(
                    identifier, at_ms=int(at_ms), time_basis="native"
                )
            except Exception:  # pragma: no cover - defensive
                contact = None
            if contact:
                break
        if not contact:
            continue
        # Senders are compared by their terminal contact, so members must be too.
        contact = queries.terminal(str(contact)) or contact
        row = queries.contact(str(contact))
        if row is None:
            continue
        name = str(row["display_name"] or "").strip()
        if name:
            out[str(contact)] = name
    return out


def _outsider_names(
    queries: Any, *, chat_id: str, now_ms: int, member_ids: set[str]
) -> tuple[str, ...]:
    """Display names of recent senders who are not current members of the chat."""
    rows = queries.recent(
        chat_id=str(chat_id),
        limit=MAX_OWN_MESSAGES,
        after_ms=int(now_ms) - OBSERVATION_DAYS * DAY_MS,
    )
    found: set[str] = set()
    for row in rows:
        contact = row["sender_contact_id"]
        if not contact:
            continue
        try:
            terminal = queries.terminal(str(contact))
        except Exception:  # pragma: no cover - defensive
            terminal = None
        if terminal is not None and terminal in member_ids:
            continue
        if terminal is None:
            continue
        profile = queries.contact(terminal)
        if profile is None:
            continue
        name = str(profile["display_name"] or "").strip()
        if name:
            found.add(name)
    return tuple(sorted(found, key=str.casefold))


class PersonNotesRunner:
    """Read one person's context, call the model once, validate, then store."""

    def __init__(
        self,
        *,
        knowledge: Any,
        history: Any,
        client_factory: Callable[[], Any],
        context_factory: Callable[[Any, str, str, int], TrustedReadContext | None] | None = None,
        tz: tzinfo | None = None,
        max_lines: int = MAX_LINES_PER_NOTE,
        max_input_chars: int = MAX_INPUT_CHARS,
        max_output_tokens: int = 800,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._knowledge = knowledge
        self._history = history
        self._client_factory = client_factory
        self._context_factory = context_factory
        self._tz = tz
        self._max_lines = max(1, int(max_lines))
        self._max_input_chars = max(256, int(max_input_chars))
        self._max_output_tokens = max(64, int(max_output_tokens))
        self._timeout_seconds = max(1.0, float(timeout_seconds))

    @property
    def tz(self) -> tzinfo | None:
        return self._tz

    def client(self) -> Any:
        return self._client_factory()

    async def candidates(
        self, chat_id: str, *, now_ms: int, channel: str = "whatsapp", limit: int = 30
    ) -> tuple[tuple[str, str], ...]:
        """People who spoke recently and are still current members, newest first."""
        if self._history is None:
            return ()
        from yeoman_gateway.history.context import history_turn

        async with history_turn(self._history) as snapshot:
            from yeoman_gateway.history.queries import HistoryQueries

            queries = HistoryQueries(snapshot)
            audience = queries.members(chat_id=str(chat_id), at_ms=int(now_ms))
            if audience.status != "known" or not audience.members:
                return ()
            members = _member_contacts(queries, audience.members, at_ms=int(now_ms))
            member_ids = set(members)
            rows = queries.recent(
                chat_id=str(chat_id),
                limit=MAX_OWN_MESSAGES,
                after_ms=int(now_ms) - OBSERVATION_DAYS * DAY_MS,
            )
            seen: dict[str, str] = {}
            for row in reversed(rows):
                if str(row["direction"]) != "in" or not row["sender_contact_id"]:
                    continue
                try:
                    terminal = queries.terminal(str(row["sender_contact_id"]))
                except Exception:  # pragma: no cover - defensive
                    continue
                if terminal in member_ids and terminal not in seen:
                    seen[terminal] = members[terminal]
            return tuple(list(seen.items())[: max(0, int(limit))])

    async def plan(
        self,
        chat_id: str,
        person_id: str,
        *,
        now_ms: int,
        channel: str = "whatsapp",
        since_ms: int | None = None,
    ) -> PersonNotePlan | None:
        """Read under one lease; the provider call happens later, outside it."""
        if channel == "whatsapp" and self._history is not None:
            from yeoman_gateway.history.context import history_knowledge_scope, history_turn

            async with history_turn(self._history) as snapshot:
                with history_knowledge_scope(snapshot, self._knowledge):
                    from yeoman_gateway.history.queries import HistoryQueries

                    return self._plan_with(
                        HistoryQueries(snapshot),
                        chat_id,
                        person_id,
                        now_ms=now_ms,
                        channel=channel,
                        since_ms=since_ms,
                    )
        return self._plan_with(
            None, chat_id, person_id, now_ms=now_ms, channel=channel, since_ms=since_ms
        )

    def _plan_with(
        self,
        queries: Any,
        chat_id: str,
        person_id: str,
        *,
        now_ms: int,
        channel: str,
        since_ms: int | None,
    ) -> PersonNotePlan | None:
        if queries is not None:
            audience = queries.members(chat_id=str(chat_id), at_ms=int(now_ms))
            if audience.status != "known" or not audience.members:
                return None
            principal = sorted(audience.members)[0]
            context = build_group_read_context(
                queries=queries,
                channel=channel,
                chat_id=str(chat_id),
                principal=principal,
                is_owner=False,
                at_ms=int(now_ms),
                policy_revision=int(self._knowledge.policy_revision),
                purpose="reply",
            )
        elif self._context_factory is not None:
            context = self._context_factory(None, channel, str(chat_id), int(now_ms))
        else:
            return None
        if context is None:
            return None
        person_name = self._knowledge.display_name(str(person_id), context=context, for_group=True)
        if not person_name:
            return None
        stated = stated_lines(
            self._knowledge, str(person_id), context=context, limit=self._max_lines
        )
        if queries is None:
            return PersonNotePlan(
                channel=channel,
                chat_id=str(chat_id),
                person_id=str(person_id),
                person_name=str(person_name),
                input_hash=_hash(
                    {
                        "person": str(person_id),
                        "stated": [(item.line.refs, item.revision) for item in stated],
                        "observed": [],
                    }
                ),
                input_cut_ms=int(now_ms),
                stated=stated,
                observed=build_observed_input(str(person_name), ()),
                member_names=(),
                outsider_names=(),
            )
        own, new = _own_messages(
            queries,
            chat_id=str(chat_id),
            person_id=str(person_id),
            now_ms=int(now_ms),
            since_ms=since_ms,
        )
        if not own and not stated:
            return None
        members = _member_contacts(queries, audience.members, at_ms=int(now_ms))
        member_names = tuple(sorted(set(members.values()), key=str.casefold))
        outsiders = _outsider_names(
            queries, chat_id=str(chat_id), now_ms=int(now_ms), member_ids=set(members)
        )
        known = [(message_id, text) for message_id, text in own]
        observed = build_observed_input(str(person_name), known, max_chars=self._max_input_chars)
        fingerprints = _message_fingerprints(queries, observed)
        return PersonNotePlan(
            channel=channel,
            chat_id=str(chat_id),
            person_id=str(person_id),
            person_name=str(person_name),
            input_hash=_hash(
                {
                    "person": str(person_id),
                    "stated": [(item.line.refs, item.revision) for item in stated],
                    "observed": fingerprints,
                }
            ),
            input_cut_ms=int(now_ms),
            stated=stated,
            observed=observed,
            member_names=member_names,
            outsider_names=outsiders,
            new_messages=new,
        )

    async def execute(
        self, plan: PersonNotePlan, *, client: Any | None = None
    ) -> PersonNoteOutcome:
        """Make at most one model call, validate it, then store the new version."""
        observed = plan.observed
        calls = 0
        usage: dict[str, Any] = {}
        observed_lines: tuple[PersonNoteLine, ...] = ()
        dropped = 0
        if observed.messages:
            try:
                route = client if client is not None else self.client()
            except RouteUnavailableError:
                return PersonNoteOutcome("failed", "route_unavailable")
            calls = 1
            messages = observed_messages(observed.payload, plan.person_name)
            try:
                reply = await asyncio.wait_for(
                    route.chat_with_usage(
                        messages,
                        max_tokens=self._max_output_tokens,
                        response_format=_response_format(route),
                        timeout_ms=int(self._timeout_seconds * 1000),
                    ),
                    timeout=self._timeout_seconds,
                )
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                return PersonNoteOutcome("failed", "timeout", model_calls=calls)
            except Exception as exc:  # noqa: BLE001 - provider failures are a retry
                logger.warning(
                    "person_notes_provider_failed chat={} person={} error_type={}",
                    _short(plan.chat_id),
                    _short(plan.person_id),
                    type(exc).__name__,
                )
                return PersonNoteOutcome("failed", "provider_error", model_calls=calls)
            usage = dict(getattr(reply, "usage", {}) or {})
            try:
                parsed = parse_observed_lines(
                    str(getattr(reply, "content", "") or ""),
                    observed=observed,
                    member_names=plan.member_names,
                    outsider_names=plan.outsider_names,
                    max_lines=MAX_LINES_PER_NOTE,
                )
            except ValueError:
                return PersonNoteOutcome("failed", "invalid_output", model_calls=calls, usage=usage)
            observed_lines = parsed.lines
            dropped = parsed.dropped
        lines: list[PersonNoteLine] = [item.line for item in plan.stated]
        lines.extend(observed_lines)
        lines = lines[: self._max_lines]
        if not lines:
            return PersonNoteOutcome("skipped", "no_lines", model_calls=calls, usage=usage)
        revisions = {
            str(item.line.refs[0]): int(item.revision) for item in plan.stated if item.line.refs
        }
        sources: list[tuple[str, str, int]] = []
        for line in lines:
            if line.kind == "stated" and line.refs:
                ref = str(line.refs[0])
                sources.append(("statement", ref, revisions.get(ref, 0)))
            elif line.kind == "observed":
                sources.extend(("message", str(ref), 0) for ref in line.refs)
        view = self._knowledge.store_person_note(
            PersonNoteSeed(
                channel=plan.channel,
                chat_id=plan.chat_id,
                person_id=plan.person_id,
                input_hash=plan.input_hash,
                input_cut_ms=plan.input_cut_ms,
                lines=tuple(lines),
                sources=tuple(dict.fromkeys(sources)),
            )
        )
        logger.info(
            "person_note_stored chat={} person={} note={} lines={} dropped={}"
            " input_tokens_est={} usage={}",
            _short(plan.chat_id),
            _short(plan.person_id),
            view.note_id,
            len(lines),
            dropped,
            plan.estimated_tokens,
            usage,
        )
        return PersonNoteOutcome(
            "done", "", note=view, model_calls=calls, usage=usage, dropped=dropped
        )


def _message_fingerprints(queries: Any, observed: ObservedInput) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for message in observed.messages:
        try:
            fingerprint = queries.content_fingerprint(message.message_id) or ""
        except Exception:  # pragma: no cover - defensive
            fingerprint = ""
        out.append((message.message_id, str(fingerprint)))
    return out


class PersonNotesJob:
    """The daily tick: recover, plan due people, respect the budget, run one call each."""

    def __init__(
        self,
        *,
        runner: PersonNotesRunner,
        knowledge: Any,
        chats: Callable[[], Iterable[str]],
        settings: Any | Callable[[], Any],
        clock_ms: Callable[[], int] | None = None,
        max_attempts: int = MAX_ATTEMPTS,
        jobs_per_tick: int = JOBS_PER_TICK,
    ) -> None:
        self._runner = runner
        self._knowledge = knowledge
        self._chats = chats
        self._settings = settings
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._max_attempts = max(1, int(max_attempts))
        self._jobs_per_tick = max(1, int(jobs_per_tick))
        self._route_missing_day: str | None = None
        self._running = False
        self._task: Any | None = None
        self._interval_seconds = 3_600

    def _config(self) -> Any:
        return self._settings() if callable(self._settings) else self._settings

    async def start(self) -> None:
        """Run the tick as its own bounded task on the gateway loop."""
        if getattr(self, "_running", False):
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._running = False
        task = getattr(self, "_task", None)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError, Exception:  # noqa: BLE001 - shutdown path
                pass
            self._task = None

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - notes never crash the gateway
                logger.warning("person_notes_tick_failed error_type={}", type(exc).__name__)
            try:
                await asyncio.sleep(self._interval_seconds)
            except asyncio.CancelledError:
                raise

    def _day(self, now_ms: int) -> str:
        return _day(now_ms, self._runner.tz)

    async def tick(self, *, now_ms: int | None = None) -> PersonNoteTickReport:
        moment = int(now_ms if now_ms is not None else self._clock_ms())
        settings = self._config()
        report = PersonNoteTickReport()
        try:
            client = self._runner.client()
        except RouteUnavailableError as exc:
            report.route_missing = True
            day = self._day(moment)
            if self._route_missing_day != day:
                self._route_missing_day = day
                logger.warning(
                    "person_notes_route_missing route={} detail={}",
                    PERSON_NOTES_ROUTE,
                    str(exc)[:160],
                )
            return report

        report.recovered = len(
            self._knowledge.recover_person_note_jobs(
                updated_before_ms=moment - STALE_RUNNING_MS, now_ms=moment
            )
        )
        rebuild_ms = int(getattr(settings, "rebuild_every_seconds", 86_400)) * 1000
        min_new = int(getattr(settings, "min_new_messages", 5))
        max_people = int(getattr(settings, "max_people_per_chat", 30))
        cap = int(getattr(settings, "daily_prompt_token_cap", 300_000))
        day = self._day(moment)

        plans: list[PersonNotePlan] = []
        for chat_id in dict.fromkeys(str(item) for item in self._chats() if str(item)):
            for person_id, _name in await self._runner.candidates(
                chat_id, now_ms=moment, limit=max_people
            ):
                latest = self._knowledge.latest_person_note(
                    channel="whatsapp", chat_id=chat_id, person_id=person_id
                )
                since = None
                if latest is not None and latest.status == "active":
                    if moment - int(latest.built_ms) < rebuild_ms:
                        continue
                    since = int(latest.built_ms)
                plan = await self._runner.plan(chat_id, person_id, now_ms=moment, since_ms=since)
                if plan is None:
                    continue
                fresh = plan.new_messages if since is not None else len(plan.observed.messages)
                if plan.observed.messages and fresh < min_new:
                    continue
                if self._knowledge.has_person_note_hash(
                    channel=plan.channel,
                    chat_id=plan.chat_id,
                    person_id=plan.person_id,
                    input_hash=plan.input_hash,
                ):
                    continue
                plans.append(plan)

        for plan in plans:
            job = self._knowledge.enqueue_person_note(
                channel=plan.channel,
                chat_id=plan.chat_id,
                person_id=plan.person_id,
                input_hash=plan.input_hash,
                now_ms=moment,
            )
            report.enqueued += 1 if job.created else 0
            claimed = self._knowledge.claim_person_note_job(
                job.job_id, max_attempts=self._max_attempts, now_ms=moment
            )
            if claimed is None:
                continue
            used = self._knowledge.person_note_tokens(day=day)
            if used + plan.estimated_tokens > cap:
                report.skipped_budget += 1
                report.outcomes["skipped_budget"] = report.outcomes.get("skipped_budget", 0) + 1
                self._knowledge.finish_person_note_job(
                    claimed.job_id,
                    state="skipped",
                    reason="skipped_budget",
                    now_ms=moment,
                    due_ms=moment + rebuild_ms,
                )
                continue
            report.ran += 1
            outcome = await self._runner.execute(plan, client=client)
            report.outcomes[outcome.state] = report.outcomes.get(outcome.state, 0) + 1
            prompt_tokens = int(
                (outcome.usage or {}).get("prompt_tokens")
                or (outcome.usage or {}).get("input_tokens")
                or 0
            )
            if prompt_tokens:
                self._knowledge.add_person_note_tokens(day=day, tokens=prompt_tokens)
            self._record(claimed, outcome, now_ms=moment)
        return report

    def _record(self, job: Any, outcome: PersonNoteOutcome, *, now_ms: int) -> None:
        finish = self._knowledge.finish_person_note_job
        if outcome.state == "done":
            finish(job.job_id, state="done", now_ms=now_ms)
        elif outcome.state == "skipped":
            finish(job.job_id, state="skipped", reason=outcome.reason, now_ms=now_ms)
        else:
            retry = job.attempts < self._max_attempts
            finish(
                job.job_id,
                state="failed",
                reason=outcome.reason,
                now_ms=now_ms,
                due_ms=now_ms + RETRY_BACKOFF_MS * 2 ** max(0, job.attempts - 1) if retry else None,
            )
        logger.info(
            "person_note_job chat={} person={} state={} reason={} attempt={}",
            _short(job.chat_id),
            _short(job.person_id),
            outcome.state,
            outcome.reason or "-",
            job.attempts,
        )


__all__ = [
    "MAX_ATTEMPTS",
    "PERSON_NOTES_ROUTE",
    "PersonNoteOutcome",
    "PersonNotePlan",
    "PersonNoteTickReport",
    "PersonNotesJob",
    "PersonNotesRunner",
    "PlannedLine",
    "stated_lines",
]
