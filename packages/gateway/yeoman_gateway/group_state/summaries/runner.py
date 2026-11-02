"""One bounded summary pass: ended conversations, then closed weeks.

The model call never runs under a store/history write and never under the group-state
lock. A conversation summary is built from that conversation's messages only; a week
summary is built from that week's stored conversation summaries only, so it never
unions 500 message sources. Deleting a source message drops the conversation item and
cascades to any week that depends on it.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from typing import Any, Protocol

from loguru import logger

from yeoman_gateway.history.context import history_turn
from yeoman_gateway.history.conversations import (
    Conversation,
    ConversationBounds,
    read_conversations,
)
from yeoman_gateway.history.ids import PN_SUFFIX
from yeoman_gateway.history.queries import HistoryQueries
from yeoman_gateway.processing.model_route import RouteUnavailableError

from ..models import Item, Section
from ..sections._common import names_for
from ..sections.past import PAST_SECTION, closed_week_bounds, prune_past_items
from ..store import GroupStateStore
from .parse import ParsedSummary, SummaryParseError, parse_summary_lines
from .prompt import (
    MAX_CONVERSATION_LINES,
    MAX_OUTPUT_TOKENS,
    MAX_WEEK_LINES,
    PROMPT_VERSION,
    SummaryInput,
    build_conversation_input,
    build_week_input,
)

SUMMARIES_KIND = "summaries"
TTL_MS = 7 * 24 * 60 * 60 * 1000
MAX_ATTEMPTS = 2
MAX_INPUT_ROWS = 500
DAY_MS = 24 * 60 * 60 * 1000
#: Own-message window used to find people who are no longer current members.
OUTSIDER_WINDOW_DAYS = 7
MAX_OUTSIDER_ROWS = 300

_UNAVAILABLE_LOGGED = False


class ChatRoute(Protocol):
    async def chat_with_usage(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        response_format: dict[str, Any] | None = None,
        timeout_ms: int | None = None,
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class SummaryRunOutcome:
    outcome: str
    conversation_summaries: int
    week_summaries: int
    attempts: int
    prompt_tokens: int
    completion_tokens: int


@dataclass(frozen=True, slots=True)
class SummaryPreview:
    conversations: int
    weeks: int
    estimated_prompt_tokens: int


@dataclass(frozen=True, slots=True)
class _PendingWeek:
    iso_week: str
    start_ms: int
    end_ms: int


class ConversationSummarizer:
    """Build the next missing ended-conversation and closed-week summaries."""

    def __init__(
        self,
        *,
        store: GroupStateStore | None,
        history_projector: Any,
        settings: Any,
        group_state_settings: Any,
        client_factory: Callable[[], ChatRoute],
        consciousness: Any | None = None,
        clock: Callable[[], float] = time.time,
        tz: tzinfo | None = None,
    ) -> None:
        self._store = store
        self._history_projector = history_projector
        self._settings = settings
        self._group_state_settings = group_state_settings
        self._client_factory = client_factory
        self._consciousness = consciousness
        self._clock = clock
        self._tz = tz

    @property
    def tz(self) -> tzinfo | None:
        return self._tz

    async def run(
        self,
        channel: str,
        chat_id: str,
        *,
        lock: asyncio.Lock | None = None,
        now_ms: int | None = None,
    ) -> SummaryRunOutcome:
        """One pass: new ended conversations first, then any missing closed week."""
        moment = int(now_ms) if now_ms is not None else int(self._clock() * 1000)
        attempts = prompt_tokens = completion_tokens = 0
        conversations_written = weeks_written = 0
        any_written = False
        try:
            conversations = await self._read_conversations(chat_id, moment)
            summarized = self._summarized_conversations(channel, chat_id)
            cursor_ms, cursor_id = self._cursor(channel, chat_id)
            pending = [
                conversation
                for conversation in conversations
                if conversation.state == "ended"
                and conversation.message_count >= int(self._settings.min_messages)
                and conversation.id not in summarized
                and (conversation.last_ms, conversation.id) > (cursor_ms, cursor_id)
            ]
            pending.sort(key=lambda conversation: (conversation.last_ms, conversation.id))
            for conversation in pending:
                if self._budget_exhausted(channel, chat_id, moment):
                    return SummaryRunOutcome(
                        "skipped_budget",
                        conversations_written,
                        weeks_written,
                        attempts,
                        prompt_tokens,
                        completion_tokens,
                    )
                summary_input = await self._conversation_input(conversation, chat_id, moment)
                if summary_input is None:
                    self._advance_cursor(channel, chat_id, conversation)
                    continue
                if self._cache_hit(channel, chat_id, summary_input):
                    self._advance_cursor(channel, chat_id, conversation)
                    continue
                route = self._client(channel, chat_id, conversation.id)
                if route is None:
                    return SummaryRunOutcome(
                        "unavailable",
                        conversations_written,
                        weeks_written,
                        attempts,
                        prompt_tokens,
                        completion_tokens,
                    )
                result, parsed = await self._attempts(
                    route,
                    summary_input,
                    channel,
                    chat_id,
                    conversation,
                    moment,
                    lock=lock,
                )
                attempts += result.attempts
                prompt_tokens += result.prompt_tokens
                completion_tokens += result.completion_tokens
                if result.outcome == "written" and parsed is not None:
                    conversations_written += 1
                    any_written = True
                    self._advance_cursor(channel, chat_id, conversation)
                    continue
                if result.outcome == "skipped_budget":
                    return SummaryRunOutcome(
                        "skipped_budget",
                        conversations_written,
                        weeks_written,
                        attempts,
                        prompt_tokens,
                        completion_tokens,
                    )
                # A failed call keeps the cursor so the next tick retries this conversation.
                return SummaryRunOutcome(
                    result.outcome,
                    conversations_written,
                    weeks_written,
                    attempts,
                    prompt_tokens,
                    completion_tokens,
                )

            for week in self._pending_weeks(channel, chat_id, moment):
                if self._budget_exhausted(channel, chat_id, moment):
                    return SummaryRunOutcome(
                        "skipped_budget",
                        conversations_written,
                        weeks_written,
                        attempts,
                        prompt_tokens,
                        completion_tokens,
                    )
                week_input = self._week_input(channel, chat_id, week)
                if week_input is None:
                    continue
                if self._cache_hit(channel, chat_id, week_input):
                    continue
                route = self._client(channel, chat_id, week.iso_week)
                if route is None:
                    return SummaryRunOutcome(
                        "unavailable",
                        conversations_written,
                        weeks_written,
                        attempts,
                        prompt_tokens,
                        completion_tokens,
                    )
                result, parsed = await self._attempts(
                    route,
                    week_input,
                    channel,
                    chat_id,
                    None,
                    moment,
                    lock=lock,
                    week=week,
                )
                attempts += result.attempts
                prompt_tokens += result.prompt_tokens
                completion_tokens += result.completion_tokens
                if result.outcome == "written" and parsed is not None:
                    weeks_written += 1
                    any_written = True
                    continue
                return SummaryRunOutcome(
                    result.outcome,
                    conversations_written,
                    weeks_written,
                    attempts,
                    prompt_tokens,
                    completion_tokens,
                )
        except Exception as exc:  # noqa: BLE001 - the ticker keeps running
            logger.warning(
                "group_state_summaries_failed chat={}:{} error_type={}",
                channel,
                chat_id,
                type(exc).__name__,
            )
            return SummaryRunOutcome(
                "error",
                conversations_written,
                weeks_written,
                attempts,
                prompt_tokens,
                completion_tokens,
            )
        outcome = "written" if any_written else "idle"
        logger.info(
            "group_state_summaries chat={}:{} outcome={} conversations={} weeks={}"
            " attempts={} tokens={}",
            channel,
            chat_id,
            outcome,
            conversations_written,
            weeks_written,
            attempts,
            prompt_tokens + completion_tokens,
        )
        return SummaryRunOutcome(
            outcome,
            conversations_written,
            weeks_written,
            attempts,
            prompt_tokens,
            completion_tokens,
        )

    async def preview(
        self, channel: str, chat_id: str, *, now_ms: int | None = None
    ) -> SummaryPreview:
        """Counts and a token estimate only; no model call, no write."""
        moment = int(now_ms) if now_ms is not None else int(self._clock() * 1000)
        conversations = await self._read_conversations(chat_id, moment)
        summarized = self._summarized_conversations(channel, chat_id)
        cursor_ms, cursor_id = self._cursor(channel, chat_id)
        tokens = 0
        conversation_count = 0
        for conversation in conversations:
            if (
                conversation.state != "ended"
                or conversation.message_count < int(self._settings.min_messages)
                or conversation.id in summarized
                or (conversation.last_ms, conversation.id) <= (cursor_ms, cursor_id)
            ):
                continue
            summary_input = await self._conversation_input(conversation, chat_id, moment)
            if summary_input is None:
                continue
            conversation_count += 1
            tokens += summary_input.estimated_tokens
        week_count = 0
        for week in self._pending_weeks(channel, chat_id, moment):
            week_input = self._week_input(channel, chat_id, week)
            if week_input is None:
                continue
            week_count += 1
            tokens += week_input.estimated_tokens
        return SummaryPreview(
            conversations=conversation_count,
            weeks=week_count,
            estimated_prompt_tokens=tokens,
        )

    async def _read_conversations(self, chat_id: str, now_ms: int) -> list[Conversation]:
        window_hours = max(1, int(getattr(self._group_state_settings, "window_hours", 24)))
        consciousness = self._consciousness
        bounds = ConversationBounds(
            window_ms=window_hours * 3_600_000,
            max_rows=MAX_INPUT_ROWS,
            lull_ms=max(1, int(getattr(consciousness, "lull_silence_minutes", 25))) * 60_000,
            burst_n=max(1, int(getattr(consciousness, "burst_threshold_messages", 8))),
            burst_ms=max(1, int(getattr(consciousness, "burst_window_minutes", 15))) * 60_000,
        )
        async with history_turn(self._history_projector) as snapshot:
            return read_conversations(HistoryQueries(snapshot), chat_id, now_ms, bounds)

    async def _conversation_input(
        self, conversation: Conversation, chat_id: str, now_ms: int
    ) -> SummaryInput | None:
        async with history_turn(self._history_projector) as snapshot:
            queries = HistoryQueries(snapshot)
            rows = queries.recent(
                chat_id=chat_id,
                limit=MAX_INPUT_ROWS,
                after_ms=conversation.start_ms - 1,
                until_ms=conversation.last_ms,
            )
            contacts = {
                str(row.get("sender_contact_id") or "")
                for row in rows
                if row.get("direction") != "out"
            }
            names = names_for(queries, (contact for contact in contacts if contact))
            member_names, outsider_names = _audience_names(queries, chat_id, now_ms)
            return build_conversation_input(
                rows,
                conversation_id=conversation.id,
                message_ids=conversation.message_ids,
                fingerprint_of=queries.content_fingerprint,
                names=names,
                assistant_ids=queries.assistant_contact_ids(),
                max_chars=int(getattr(self._settings, "max_input_chars", 12_000)),
                min_messages=max(1, int(self._settings.min_messages)),
                member_names=member_names,
                outsider_names=outsider_names,
            )

    def _summarized_conversations(self, channel: str, chat_id: str) -> set[str]:
        return {
            str(item.conversation_id)
            for item in self._past_items(channel, chat_id)
            if item.kind == "conversation_summary" and item.conversation_id
        }

    def _past_items(self, channel: str, chat_id: str) -> tuple[Item, ...]:
        if self._store is None:
            return ()
        state = self._store.get(channel, chat_id)
        if state is None:
            return ()
        section = state.sections.get(PAST_SECTION)
        return section.items if section is not None else ()

    def _cursor(self, channel: str, chat_id: str) -> tuple[int, str]:
        if self._store is None:
            return (0, "")
        return self._store.summary_cursor(channel, chat_id)

    def _advance_cursor(self, channel: str, chat_id: str, conversation: Conversation) -> None:
        if self._store is not None:
            self._store.set_summary_cursor(channel, chat_id, conversation.last_ms, conversation.id)

    def _cache_hit(self, channel: str, chat_id: str, summary_input: SummaryInput) -> bool:
        if self._store is None:
            return False
        return self._store.run_cache_hit(
            channel, chat_id, kind=SUMMARIES_KIND, input_hash=summary_input.input_hash
        )

    def _client(self, channel: str, chat_id: str, key: str) -> ChatRoute | None:
        try:
            return self._client_factory()
        except RouteUnavailableError:
            self._log_unavailable_once()
            return None
        except Exception as exc:  # noqa: BLE001 - a broken factory is one bounded failure
            logger.warning(
                "group_state_summaries_client_failed chat={}:{} key={} error_type={}",
                channel,
                chat_id,
                key,
                type(exc).__name__,
            )
            return None

    async def _attempts(
        self,
        route: ChatRoute,
        summary_input: SummaryInput,
        channel: str,
        chat_id: str,
        conversation: Conversation | None,
        now_ms: int,
        *,
        lock: asyncio.Lock | None,
        week: _PendingWeek | None = None,
    ) -> tuple[SummaryRunOutcome, ParsedSummary | None]:
        messages = list(summary_input.messages)
        attempts = prompt_tokens = completion_tokens = 0
        model = str(getattr(route, "model", "") or getattr(self._settings, "route", ""))
        timeout = max(0.001, float(getattr(self._settings, "timeout_seconds", 30.0)))
        max_lines = MAX_WEEK_LINES if summary_input.kind == "week" else MAX_CONVERSATION_LINES
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if self._budget_exhausted(channel, chat_id, now_ms):
                return (
                    SummaryRunOutcome(
                        "skipped_budget", 0, 0, attempts, prompt_tokens, completion_tokens
                    ),
                    None,
                )
            attempts += 1
            try:
                reply = await asyncio.wait_for(
                    route.chat_with_usage(
                        messages,
                        max_tokens=MAX_OUTPUT_TOKENS,
                        response_format={"type": "json_object"},
                        timeout_ms=int(timeout * 1000),
                    ),
                    timeout=timeout,
                )
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                self._record_attempt(
                    channel,
                    chat_id,
                    summary_input,
                    conversation,
                    week,
                    attempt,
                    now_ms,
                    model,
                    "timeout",
                    0,
                    0,
                )
                return (
                    SummaryRunOutcome("timeout", 0, 0, attempts, prompt_tokens, completion_tokens),
                    None,
                )
            except Exception as exc:  # noqa: BLE001 - one failed call is a bounded retry
                logger.warning(
                    "group_state_summaries_call_failed chat={}:{} key={} error_type={} attempt={}",
                    channel,
                    chat_id,
                    summary_input.key,
                    type(exc).__name__,
                    attempt,
                )
                self._record_attempt(
                    channel,
                    chat_id,
                    summary_input,
                    conversation,
                    week,
                    attempt,
                    now_ms,
                    model,
                    "error",
                    0,
                    0,
                )
                return (
                    SummaryRunOutcome("error", 0, 0, attempts, prompt_tokens, completion_tokens),
                    None,
                )
            prompt, completion = _usage(reply)
            prompt_tokens += prompt
            completion_tokens += completion
            try:
                parsed = parse_summary_lines(
                    str(getattr(reply, "content", "") or ""),
                    ref_map=summary_input.ref_map,
                    max_lines=max_lines,
                    member_names=summary_input.member_names,
                    outsider_names=summary_input.outsider_names,
                )
                if not parsed.lines:
                    raise SummaryParseError("no_lines")
            except SummaryParseError as exc:
                self._record_attempt(
                    channel,
                    chat_id,
                    summary_input,
                    conversation,
                    week,
                    attempt,
                    now_ms,
                    model,
                    "invalid",
                    prompt,
                    completion,
                )
                if attempt < MAX_ATTEMPTS:
                    messages = [
                        *messages,
                        {
                            "role": "user",
                            "content": (
                                f"The previous answer was invalid: {exc}. Return only the "
                                "JSON object with a non-empty lines list and valid refs."
                            ),
                        },
                    ]
                    continue
                return (
                    SummaryRunOutcome("invalid", 0, 0, attempts, prompt_tokens, completion_tokens),
                    None,
                )
            if lock is None:
                outcome = self._finalize(
                    channel, chat_id, summary_input, parsed, now_ms, conversation, week
                )
            else:
                async with lock:
                    outcome = self._finalize(
                        channel, chat_id, summary_input, parsed, now_ms, conversation, week
                    )
            self._record_attempt(
                channel,
                chat_id,
                summary_input,
                conversation,
                week,
                attempt,
                now_ms,
                model,
                outcome,
                prompt,
                completion,
            )
            return (
                SummaryRunOutcome(
                    outcome,
                    1 if outcome == "written" and summary_input.kind == "conversation" else 0,
                    1 if outcome == "written" and summary_input.kind == "week" else 0,
                    attempts,
                    prompt_tokens,
                    completion_tokens,
                ),
                parsed,
            )
        return (
            SummaryRunOutcome("error", 0, 0, attempts, prompt_tokens, completion_tokens),
            None,
        )

    def _finalize(
        self,
        channel: str,
        chat_id: str,
        summary_input: SummaryInput,
        parsed: ParsedSummary,
        now_ms: int,
        conversation: Conversation | None,
        week: _PendingWeek | None,
    ) -> str:
        if self._store is None:
            return "purged"
        if summary_input.kind == "week":
            if week is None:
                return "invalid"
            item = _week_item(
                week.iso_week,
                tuple(str(value) for value in summary_input.ref_map.values()),
                parsed,
                now_ms,
            )
            return self._write_item(channel, chat_id, item, now_ms)
        if conversation is None:
            return "invalid"
        item = _conversation_item(conversation, summary_input, parsed, now_ms)
        return self._write_item(
            channel,
            chat_id,
            item,
            now_ms,
            source_cursor=conversation.message_ids[-1],
            period_start_ms=conversation.start_ms,
            period_end_ms=conversation.last_ms,
        )

    def _write_item(
        self,
        channel: str,
        chat_id: str,
        item: Item,
        now_ms: int,
        *,
        source_cursor: str = "",
        period_start_ms: int | None = None,
        period_end_ms: int | None = None,
    ) -> str:
        if self._store is None:
            return "purged"
        state = self._store.get(channel, chat_id)
        if state is None:
            return "purged"
        previous = state.sections.get(PAST_SECTION)
        current = previous.items if previous is not None else ()
        if any(existing.id == item.id for existing in current):
            return "written"
        keep_weeks = max(1, int(getattr(self._settings, "keep_weeks", 12)))
        items = prune_past_items(
            (*current, item), now_ms=now_ms, keep_weeks=keep_weeks, tz=self._tz
        )
        section = Section(
            key=PAST_SECTION,
            schema_version=1,
            trust="derived",
            layer="week",
            produced_at_ms=now_ms,
            ttl_ms=TTL_MS,
            source_cursor=source_cursor or (previous.source_cursor if previous is not None else ""),
            period_start_ms=period_start_ms
            if period_start_ms is not None
            else (previous.period_start_ms if previous is not None else None),
            period_end_ms=period_end_ms
            if period_end_ms is not None
            else (previous.period_end_ms if previous is not None else None),
            sources=(),
            data={},
            items=items,
        )
        written = self._store.merge_sections(
            channel,
            chat_id,
            {PAST_SECTION: section},
            updated_ms=now_ms,
            require_existing=True,
        )
        return "written" if written is not None else "purged"

    def _pending_weeks(self, channel: str, chat_id: str, now_ms: int) -> list[_PendingWeek]:
        items = self._past_items(channel, chat_id)
        conversations = [item for item in items if item.kind == "conversation_summary"]
        if not conversations:
            return []
        existing = {
            str(item.data.get("iso_week") or "") for item in items if item.kind == "week_summary"
        }
        keep_weeks = max(1, int(getattr(self._settings, "keep_weeks", 12)))
        pending: list[_PendingWeek] = []
        for label, start_ms, end_ms in closed_week_bounds(
            now_ms, keep_weeks=keep_weeks, tz=self._tz
        ):
            if label in existing:
                continue
            if not any(
                start_ms <= int(item.data.get("ended_ms") or 0) < end_ms for item in conversations
            ):
                continue
            pending.append(_PendingWeek(label, start_ms, end_ms))
        return pending

    def _week_input(self, channel: str, chat_id: str, week: _PendingWeek) -> SummaryInput | None:
        conversations = [
            dict(item.data)
            for item in self._past_items(channel, chat_id)
            if item.kind == "conversation_summary"
            and week.start_ms <= int(item.data.get("ended_ms") or 0) < week.end_ms
        ]
        conversations.sort(key=lambda data: int(data.get("ended_ms") or 0))
        participants: list[str] = []
        for conversation in conversations:
            for name in conversation.get("participants") or ():
                clean = str(name)
                if clean and clean not in participants:
                    participants.append(clean)
        return build_week_input(
            week.iso_week,
            conversations,
            max_chars=int(getattr(self._settings, "max_input_chars", 12_000)),
            member_names=participants,
        )

    def _budget_exhausted(self, channel: str, chat_id: str, now_ms: int) -> bool:
        if self._store is None:
            return False
        cap = int(getattr(self._settings, "daily_prompt_token_cap", 300_000))
        start_ms, end_ms = _local_day_bounds(now_ms, self._tz)
        _chat_rows, _global_rows, tokens = self._store.run_usage(
            channel, chat_id, kind=SUMMARIES_KIND, start_ms=start_ms, end_ms=end_ms
        )
        return tokens >= cap

    def _record_attempt(
        self,
        channel: str,
        chat_id: str,
        summary_input: SummaryInput,
        conversation: Conversation | None,
        week: _PendingWeek | None,
        attempt: int,
        now_ms: int,
        model: str,
        outcome: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> None:
        if self._store is None:
            return
        key = (
            conversation.id
            if conversation is not None
            else (week.iso_week if week else summary_input.key)
        )
        self._store.record_run(
            channel,
            chat_id,
            run_id=f"{key}:{summary_input.input_hash[:12]}:{attempt}",
            kind=SUMMARIES_KIND,
            started_ms=now_ms,
            input_hash=summary_input.input_hash,
            prompt_version=PROMPT_VERSION,
            model=model,
            outcome=outcome,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    def _log_unavailable_once(self) -> None:
        global _UNAVAILABLE_LOGGED
        if _UNAVAILABLE_LOGGED:
            return
        _UNAVAILABLE_LOGGED = True
        logger.warning("group_state_summaries_unavailable route={}", str(self._settings.route))


def _conversation_item(
    conversation: Conversation, summary_input: SummaryInput, parsed: ParsedSummary, now_ms: int
) -> Item:
    return Item(
        id=f"{conversation.id}:summary",
        kind="conversation_summary",
        data={
            "conversation_id": conversation.id,
            "started_ms": conversation.start_ms,
            "ended_ms": conversation.last_ms,
            "participants": list(summary_input.participants),
            "lines": [line.text for line in parsed.lines],
        },
        sources=tuple(conversation.message_ids),
        source_fingerprints=dict(summary_input.fingerprints),
        produced_at_ms=now_ms,
        ttl_ms=TTL_MS,
        trust="derived",
        conversation_id=conversation.id,
    )


def _week_item(
    iso_week: str, depends_on: Sequence[str], parsed: ParsedSummary, now_ms: int
) -> Item:
    return Item(
        id=f"week:{iso_week}",
        kind="week_summary",
        data={
            "iso_week": iso_week,
            "lines": [line.text for line in parsed.lines],
            "depends_on": list(dict.fromkeys(str(value) for value in depends_on)),
        },
        sources=(),
        source_fingerprints={},
        produced_at_ms=now_ms,
        ttl_ms=TTL_MS,
        trust="derived",
    )


def _usage(reply: Any) -> tuple[int, int]:
    usage = getattr(reply, "usage", None) or {}
    if not isinstance(usage, Mapping):
        return 0, 0
    prompt = _token(usage.get("prompt_tokens", usage.get("input_tokens", 0)))
    completion = _token(usage.get("completion_tokens", usage.get("output_tokens", 0)))
    return prompt, completion


def _token(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except TypeError, ValueError:
        return 0


def _local_day_bounds(now_ms: int, tz: tzinfo | None) -> tuple[int, int]:
    moment = datetime.fromtimestamp(int(now_ms) / 1000, tz)
    if tz is None:
        moment = moment.astimezone()
    start = datetime(moment.year, moment.month, moment.day, tzinfo=moment.tzinfo)
    end = start + timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def _member_identifiers(principal: str) -> tuple[str, ...]:
    """Identifier forms a roster principal can resolve under (same as person notes)."""
    value = principal.partition(":")[2] or principal
    if "@" in value:
        return (value,)
    return (value + PN_SUFFIX, value + "@lid")


def _member_contacts(queries: Any, members: Sequence[str], *, at_ms: int) -> dict[str, str]:
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
    rows = queries.recent(
        chat_id=str(chat_id),
        limit=MAX_OUTSIDER_ROWS,
        after_ms=int(now_ms) - OUTSIDER_WINDOW_DAYS * DAY_MS,
    )
    found: set[str] = set()
    for row in rows:
        contact = row.get("sender_contact_id")
        if not contact:
            continue
        try:
            terminal = queries.terminal(str(contact))
        except Exception:  # pragma: no cover - defensive
            terminal = None
        if terminal is None or terminal in member_ids:
            continue
        profile = queries.contact(terminal)
        if profile is None:
            continue
        name = str(profile["display_name"] or "").strip()
        if name:
            found.add(name)
    return tuple(sorted(found, key=str.casefold))


def _audience_names(
    queries: Any, chat_id: str, now_ms: int
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    audience = queries.members(chat_id=chat_id, at_ms=int(now_ms))
    if audience.status != "known" or not audience.members:
        return (), ()
    members = _member_contacts(queries, audience.members, at_ms=int(now_ms))
    member_ids = set(members)
    outsiders = _outsider_names(queries, chat_id=chat_id, now_ms=now_ms, member_ids=member_ids)
    return tuple(members.values()), outsiders


__all__ = [
    "ConversationSummarizer",
    "SUMMARIES_KIND",
    "SummaryPreview",
    "SummaryRunOutcome",
]
