"""One bounded conversation-label call: budget, cache, repair, re-read and write."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from typing import Any, Protocol

from loguru import logger

from yeoman_gateway.history.context import history_turn
from yeoman_gateway.history.queries import HistoryQueries
from yeoman_gateway.processing.model_route import RouteUnavailableError

from ..models import Item, Section
from ..store import GroupStateStore
from .parse import LabelParseError, ParsedLabels, parse_labels
from .prompt import PROMPT_VERSION, LabelInput, build_label_input

LABELS_SECTION = "conversation_labels"
MAX_OUTPUT_TOKENS = 600
TTL_MS = 24 * 60 * 60 * 1000
MAX_ATTEMPTS = 2
#: A message window larger than the conversations section's own read cap is not needed.
MAX_INPUT_ROWS = 500

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
class LabelRunOutcome:
    outcome: str
    items: int
    attempts: int
    prompt_tokens: int
    completion_tokens: int


class ConversationLabeler:
    """Label one conversation; the model call runs outside any store/history write."""

    def __init__(
        self,
        *,
        store: GroupStateStore | None,
        history_projector: Any,
        settings: Any,
        client_factory: Callable[[], ChatRoute],
        clock: Callable[[], float] = time.time,
        tz: tzinfo | None = None,
    ) -> None:
        self._store = store
        self._history_projector = history_projector
        self._settings = settings
        self._client_factory = client_factory
        self._clock = clock
        self._tz = tz

    @property
    def tz(self) -> tzinfo | None:
        return self._tz

    @property
    def store(self) -> GroupStateStore | None:
        return self._store

    async def label(
        self,
        channel: str,
        chat_id: str,
        conversation: Mapping[str, Any],
        *,
        lock: asyncio.Lock | None = None,
    ) -> LabelRunOutcome:
        """One label pass: no call when cached or over budget, no write on a race."""
        started = time.perf_counter()
        now_ms = int(self._clock() * 1000)
        conversation_id = str(conversation.get("conversation_id") or "")
        try:
            label_input = await self._read_input(chat_id, conversation)
        except Exception as exc:  # noqa: BLE001 - the ticker keeps running
            logger.warning(
                "group_state_labels_read_failed chat={}:{} error_type={}",
                channel,
                chat_id,
                type(exc).__name__,
            )
            return self._finish(
                channel, chat_id, conversation_id, started, "error", items=0, attempts=0
            )
        if label_input is None:
            return self._finish(
                channel, chat_id, conversation_id, started, "no_input", items=0, attempts=0
            )
        if self._store is not None and self._store.label_cache_hit(
            channel, chat_id, label_input.input_hash
        ):
            return self._finish(
                channel, chat_id, conversation_id, started, "cached", items=0, attempts=0
            )
        route = self._client(channel, chat_id, conversation_id)
        if route is None:
            return self._finish(
                channel, chat_id, conversation_id, started, "unavailable", items=0, attempts=0
            )
        outcome, _ = await self._attempts(
            route,
            label_input,
            channel,
            chat_id,
            conversation,
            now_ms,
            dry_run=False,
            lock=lock,
        )
        self._log(channel, chat_id, conversation_id, outcome, started)
        return outcome

    async def preview(
        self, channel: str, chat_id: str, conversation: Mapping[str, Any]
    ) -> tuple[LabelRunOutcome, ParsedLabels | None]:
        """A dry run: the same calls and budgets, but no state and no run row."""
        started = time.perf_counter()
        now_ms = int(self._clock() * 1000)
        conversation_id = str(conversation.get("conversation_id") or "")
        try:
            label_input = await self._read_input(chat_id, conversation)
        except Exception as exc:  # noqa: BLE001 - a CLI must print a bounded failure
            logger.warning(
                "group_state_labels_read_failed chat={}:{} error_type={}",
                channel,
                chat_id,
                type(exc).__name__,
            )
            outcome = self._finish(
                channel, chat_id, conversation_id, started, "error", items=0, attempts=0
            )
            return outcome, None
        if label_input is None:
            outcome = self._finish(
                channel, chat_id, conversation_id, started, "no_input", items=0, attempts=0
            )
            return outcome, None
        route = self._client(channel, chat_id, conversation_id)
        if route is None:
            outcome = self._finish(
                channel, chat_id, conversation_id, started, "unavailable", items=0, attempts=0
            )
            return outcome, None
        outcome, parsed = await self._attempts(
            route,
            label_input,
            channel,
            chat_id,
            conversation,
            now_ms,
            dry_run=True,
            lock=None,
        )
        self._log(channel, chat_id, conversation_id, outcome, started)
        return outcome, parsed

    def _client(self, channel: str, chat_id: str, conversation_id: str) -> ChatRoute | None:
        try:
            return self._client_factory()
        except RouteUnavailableError:
            self._log_unavailable_once()
            return None
        except Exception as exc:  # noqa: BLE001 - a broken factory is one bounded failure
            logger.warning(
                "group_state_labels_client_failed chat={}:{} conversation={} error_type={}",
                channel,
                chat_id,
                conversation_id,
                type(exc).__name__,
            )
            return None

    async def _attempts(
        self,
        route: ChatRoute,
        label_input: LabelInput,
        channel: str,
        chat_id: str,
        conversation: Mapping[str, Any],
        now_ms: int,
        *,
        dry_run: bool,
        lock: asyncio.Lock | None,
    ) -> tuple[LabelRunOutcome, ParsedLabels | None]:
        messages = list(label_input.messages)
        attempts = 0
        prompt_tokens = 0
        completion_tokens = 0
        model = str(getattr(route, "model", "") or getattr(self._settings, "route", ""))
        timeout = max(0.001, float(self._settings.timeout_seconds))
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if self._budget_exhausted(channel, chat_id, now_ms):
                return (
                    LabelRunOutcome(
                        "skipped_budget", 0, attempts, prompt_tokens, completion_tokens
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
                    label_input,
                    conversation,
                    attempt,
                    now_ms,
                    model,
                    "timeout",
                    dry_run,
                    0,
                    0,
                )
                return (
                    LabelRunOutcome("timeout", 0, attempts, prompt_tokens, completion_tokens),
                    None,
                )
            except Exception as exc:  # noqa: BLE001 - one failed call is a bounded retry
                logger.warning(
                    "group_state_labels_call_failed chat={}:{} conversation={}"
                    " error_type={} attempt={}",
                    channel,
                    chat_id,
                    label_input.conversation_id,
                    type(exc).__name__,
                    attempt,
                )
                self._record_attempt(
                    channel,
                    chat_id,
                    label_input,
                    conversation,
                    attempt,
                    now_ms,
                    model,
                    "error",
                    dry_run,
                    0,
                    0,
                )
                return (
                    LabelRunOutcome("error", 0, attempts, prompt_tokens, completion_tokens),
                    None,
                )

            prompt, completion = _usage(reply)
            prompt_tokens += prompt
            completion_tokens += completion
            try:
                parsed = parse_labels(str(getattr(reply, "content", "") or ""), label_input)
            except LabelParseError as exc:
                self._record_attempt(
                    channel,
                    chat_id,
                    label_input,
                    conversation,
                    attempt,
                    now_ms,
                    model,
                    "invalid",
                    dry_run,
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
                                "JSON object with topic, mood, open_questions and plans."
                            ),
                        },
                    ]
                    continue
                return (
                    LabelRunOutcome("invalid", 0, attempts, prompt_tokens, completion_tokens),
                    None,
                )

            if dry_run:
                return (
                    LabelRunOutcome(
                        "written",
                        _item_count(parsed),
                        attempts,
                        prompt_tokens,
                        completion_tokens,
                    ),
                    parsed,
                )

            if lock is None:
                outcome, items = await self._finalize(
                    channel, chat_id, conversation, label_input, parsed, now_ms
                )
            else:
                async with lock:
                    outcome, items = await self._finalize(
                        channel, chat_id, conversation, label_input, parsed, now_ms
                    )
            self._record_attempt(
                channel,
                chat_id,
                label_input,
                conversation,
                attempt,
                now_ms,
                model,
                outcome,
                dry_run,
                prompt,
                completion,
            )
            return (
                LabelRunOutcome(outcome, items, attempts, prompt_tokens, completion_tokens),
                parsed,
            )
        return (
            LabelRunOutcome("error", 0, attempts, prompt_tokens, completion_tokens),
            None,
        )

    async def _read_input(self, chat_id: str, conversation: Mapping[str, Any]) -> LabelInput | None:
        start_ms = int(conversation.get("start_ms") or 0)
        last_ms = int(conversation.get("last_ms") or 0)
        async with history_turn(self._history_projector) as snapshot:
            queries = HistoryQueries(snapshot)
            rows = queries.recent(
                chat_id=chat_id,
                limit=MAX_INPUT_ROWS,
                after_ms=start_ms - 1,
                until_ms=last_ms,
            )
            return build_label_input(
                rows,
                conversation_id=str(conversation.get("conversation_id") or ""),
                assistant_ids=queries.assistant_contact_ids(),
                fingerprint_of=queries.content_fingerprint,
                max_chars=int(self._settings.max_input_chars),
                min_messages=int(self._settings.min_messages),
            )

    async def _sources_unchanged(self, label_input: LabelInput, parsed: ParsedLabels) -> bool:
        cited = _cited_sources(parsed, label_input)
        async with history_turn(self._history_projector) as snapshot:
            queries = HistoryQueries(snapshot)
            for source in cited:
                expected = label_input.fingerprints.get(source)
                if expected is None or queries.content_fingerprint(source) != expected:
                    return False
        return True

    async def _finalize(
        self,
        channel: str,
        chat_id: str,
        conversation: Mapping[str, Any],
        label_input: LabelInput,
        parsed: ParsedLabels,
        now_ms: int,
    ) -> tuple[str, int]:
        if not await self._sources_unchanged(label_input, parsed):
            return "source_changed", 0
        items = _build_items(label_input, parsed, now_ms)
        section = self._section(channel, chat_id, conversation, label_input, items, now_ms)
        if self._store is None:
            return "purged", 0
        written = self._store.merge_sections(
            channel,
            chat_id,
            {LABELS_SECTION: section},
            updated_ms=now_ms,
            require_existing=True,
        )
        return ("written", len(items)) if written is not None else ("purged", 0)

    def _section(
        self,
        channel: str,
        chat_id: str,
        conversation: Mapping[str, Any],
        label_input: LabelInput,
        items: tuple[Item, ...],
        now_ms: int,
    ) -> Section:
        existing = self._store.get(channel, chat_id) if self._store is not None else None
        current = str(conversation.get("conversation_id") or "")
        previous = existing.sections.get(LABELS_SECTION) if existing is not None else None
        others: dict[str | None, list[Item]] = {}
        for item in previous.items if previous is not None else ():
            if item.conversation_id == current:
                continue
            others.setdefault(item.conversation_id, []).append(item)
        capacity = max(0, int(self._settings.max_conversations) - 1)
        ranked = sorted(
            others.items(),
            key=lambda entry: max(item.produced_at_ms for item in entry[1]),
            reverse=True,
        )
        kept = [item for _, group in ranked[:capacity] for item in group]
        merged = (*kept, *items)
        sources = tuple(dict.fromkeys(source for item in merged for source in item.sources))
        message_ids = tuple(label_input.message_refs.values())
        return Section(
            key=LABELS_SECTION,
            schema_version=1,
            trust="derived",
            layer="conversation",
            produced_at_ms=now_ms,
            ttl_ms=TTL_MS,
            source_cursor=message_ids[-1] if message_ids else "",
            period_start_ms=conversation.get("start_ms"),
            period_end_ms=conversation.get("last_ms"),
            sources=sources,
            data={},
            items=merged,
        )

    def _budget_exhausted(self, channel: str, chat_id: str, now_ms: int) -> bool:
        if self._store is None:
            return False
        start_ms, end_ms = _local_day_bounds(now_ms, self._tz)
        chat_rows, global_rows, tokens = self._store.label_usage(
            channel, chat_id, start_ms=start_ms, end_ms=end_ms
        )
        return (
            chat_rows >= int(self._settings.max_per_chat_day)
            or global_rows >= int(self._settings.max_global_day)
            or tokens >= int(self._settings.max_tokens_day)
        )

    def _record_attempt(
        self,
        channel: str,
        chat_id: str,
        label_input: LabelInput,
        conversation: Mapping[str, Any],
        attempt: int,
        now_ms: int,
        model: str,
        outcome: str,
        dry_run: bool,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> None:
        if dry_run or self._store is None:
            return
        conversation_id = str(conversation.get("conversation_id") or "")
        self._store.record_run(
            channel,
            chat_id,
            run_id=f"{conversation_id}:{label_input.input_hash[:12]}:{attempt}",
            kind="labels",
            started_ms=now_ms,
            input_hash=label_input.input_hash,
            prompt_version=PROMPT_VERSION,
            model=model,
            outcome=outcome,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    def _finish(
        self,
        channel: str,
        chat_id: str,
        conversation_id: str,
        started: float,
        outcome: str,
        *,
        items: int,
        attempts: int,
    ) -> LabelRunOutcome:
        result = LabelRunOutcome(outcome, items, attempts, 0, 0)
        self._log(channel, chat_id, conversation_id, result, started)
        return result

    def _log(
        self,
        channel: str,
        chat_id: str,
        conversation_id: str,
        outcome: LabelRunOutcome,
        started: float,
    ) -> None:
        logger.info(
            "group_state_labels chat={}:{} conversation={} outcome={} items={} attempts={}"
            " tokens={} ms={}",
            channel,
            chat_id,
            conversation_id,
            outcome.outcome,
            outcome.items,
            outcome.attempts,
            int(outcome.prompt_tokens) + int(outcome.completion_tokens),
            int((time.perf_counter() - started) * 1000),
        )

    def _log_unavailable_once(self) -> None:
        global _UNAVAILABLE_LOGGED
        if _UNAVAILABLE_LOGGED:
            return
        _UNAVAILABLE_LOGGED = True
        logger.warning("group_state_labels_unavailable route={}", str(self._settings.route))


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


def _item_count(parsed: ParsedLabels) -> int:
    return (
        (1 if parsed.topic else 0)
        + (1 if parsed.mood else 0)
        + len(parsed.open_questions)
        + len(parsed.plans)
    )


def _cited_sources(parsed: ParsedLabels, label_input: LabelInput) -> set[str]:
    all_sources = set(label_input.message_refs.values())
    cited: set[str] = set()
    if parsed.topic:
        cited.update(all_sources)
    if parsed.mood:
        cited.update(all_sources)
    for question in parsed.open_questions:
        cited.add(question["source_id"])
    for plan in parsed.plans:
        cited.add(str(plan["source_id"]))
    return cited


def _build_items(label_input: LabelInput, parsed: ParsedLabels, now_ms: int) -> tuple[Item, ...]:
    conversation_id = label_input.conversation_id
    all_sources = tuple(label_input.message_refs.values())
    items: list[Item] = []
    if parsed.topic:
        items.append(
            _item(
                conversation_id,
                "topic",
                1,
                {"text": parsed.topic},
                all_sources,
                label_input,
                now_ms,
            )
        )
    if parsed.mood:
        items.append(
            _item(
                conversation_id,
                "mood",
                1,
                {"text": parsed.mood, "mood": parsed.mood},
                all_sources,
                label_input,
                now_ms,
            )
        )
    for index, question in enumerate(parsed.open_questions, start=1):
        items.append(
            _item(
                conversation_id,
                "open_question",
                index,
                {
                    "text": question["text"],
                    "asked_by": question["asked_by"],
                    "source_id": question["source_id"],
                },
                (question["source_id"],),
                label_input,
                now_ms,
            )
        )
    for index, plan in enumerate(parsed.plans, start=1):
        items.append(
            _item(
                conversation_id,
                "plan",
                index,
                {
                    "text": plan["text"],
                    "who": list(plan.get("who") or ()),
                    "when": plan.get("when") or "",
                    "source_id": plan["source_id"],
                },
                (str(plan["source_id"]),),
                label_input,
                now_ms,
            )
        )
    return tuple(items)


def _item(
    conversation_id: str,
    kind: str,
    index: int,
    data: Mapping[str, Any],
    sources: tuple[str, ...],
    label_input: LabelInput,
    now_ms: int,
) -> Item:
    unique = tuple(dict.fromkeys(source for source in sources if source))
    return Item(
        id=f"{conversation_id}:{kind}:{index}",
        kind=kind,
        data=data,
        sources=unique,
        source_fingerprints={
            source: label_input.fingerprints[source]
            for source in unique
            if source in label_input.fingerprints
        },
        produced_at_ms=now_ms,
        ttl_ms=TTL_MS,
        trust="derived",
        conversation_id=conversation_id,
    )


def _local_day_bounds(now_ms: int, tz: tzinfo | None) -> tuple[int, int]:
    moment = datetime.fromtimestamp(int(now_ms) / 1000, tz)
    if tz is None:
        moment = moment.astimezone()
    start = datetime(moment.year, moment.month, moment.day, tzinfo=moment.tzinfo)
    end = start + timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


__all__ = [
    "LABELS_SECTION",
    "ChatRoute",
    "ConversationLabeler",
    "LabelRunOutcome",
]
