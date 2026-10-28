"""Source-backed context for one participation decision.

The judge and the unsolicited generator both read from here. Two invariants matter
more than the shape of the result:

* **Same chat only.** Every query is an exact ``(channel, chat_id)`` query. A record
  from another channel that happens to share a chat id is never returned, and a
  record from another chat in the same channel is never returned.
* **No unverified statements.** Only a *delivered* Arvid message becomes an anchor.
  A draft, a preview, a submitted or transport-accepted-only effect and a historical
  ``sent`` row without recipient evidence are not things Arvid said.

The builder assembles a bounded, JSON-compatible dict and reports what it truncated.
It performs no network research or long-term personal/contact memory recall.
Knowledge comes only through the injected selector and its verified readers.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from loguru import logger

from yeoman_gateway.history.conversations import Conversation, registry_ids, render_header, segment
from yeoman_gateway.history.live import HistoryPaused
from yeoman_gateway.policy.engine import ParticipationSnapshot
from yeoman_gateway.processing.participation import (
    ParticipationDecisionError,
    ParticipationOpportunity,
)
from yeoman_gateway.processing.participation_knowledge import (
    ParticipationKnowledgeReaders,
    readers_from_single,
)
from yeoman_gateway.trends.seeds import is_trend_source

#: Keys a stored message row may use for its identity.
_ID_KEYS: tuple[str, ...] = ("message_id", "event_id", "id")
#: ``participation_conversation_fallback`` is logged at most once per chat per hour.
_FALLBACK_LOG_INTERVAL_MS = 3_600_000


@dataclass(frozen=True, slots=True)
class ParticipationContextBounds:
    """Validated bounds for one context build."""

    window_minutes: int = 120
    max_messages: int = 40
    max_anchors: int = 10
    #: ``window`` (default) or ``conversation`` (Spec A section 5.2).
    context_mode: str = "window"
    #: Live ``consciousness`` lull/burst definitions used to segment conversations.
    lull_minutes: int = 25
    burst_messages: int = 8
    burst_minutes: int = 15
    #: Conversation view floor: below this many visible conversation messages, the
    #: newest earlier window messages before it are added as marked earlier context.
    #: ``0`` disables the floor.
    conversation_min_messages: int = 8


@dataclass(frozen=True, slots=True)
class ParticipationDecisionInputs:
    """One trusted, immutable input bundle shared by preflight and context build."""

    snapshot: ParticipationSnapshot
    bounds: ParticipationContextBounds
    allowed_actions: tuple[str, ...]
    allowed_intents: frozenset[str]
    remaining_budgets: tuple[tuple[str, int], ...]
    reservation_limits_by_intent: tuple[
        tuple[str, tuple[tuple[str, int, int] | tuple[str, int, int, str], ...]], ...
    ]
    approval_required: bool
    arbitration_revision: int
    current_source_ids: tuple[str, ...]
    continuation_candidate: bool

    def __post_init__(self) -> None:
        """Normalize collection fields at the trust boundary."""
        object.__setattr__(
            self,
            "allowed_actions",
            tuple(str(item) for item in (self.allowed_actions or ())),
        )
        object.__setattr__(
            self,
            "allowed_intents",
            frozenset(str(item) for item in (self.allowed_intents or ())),
        )
        object.__setattr__(
            self,
            "remaining_budgets",
            _freeze_budget_pairs(self.remaining_budgets),
        )
        object.__setattr__(
            self,
            "reservation_limits_by_intent",
            _validate_reservation_limits(self.reservation_limits_by_intent),
        )
        object.__setattr__(
            self,
            "current_source_ids",
            tuple(str(item) for item in self.current_source_ids if str(item).strip()),
        )


class _ParticipationArchive:
    def __init__(self, adapter):
        self.adapter = adapter

    def lookup_message(self, channel, chat_id, message_id):
        row = self.adapter.row(channel, chat_id, message_id)
        return {**row, 'message_id': message_id} if row else None

    def lookup_messages_in_range(self, *args, **kwargs):
        return self.adapter.lookup_messages_in_range(*args, **kwargs)


class ParticipationContextBuilder:
    """Builds the bounded judge/generator view from archived sources and anchors."""

    def __init__(
        self,
        *,
        policy: Any,
        anchors: Any | None = None,
        clock: Any | None = None,
        source_authorizer: Callable[[Mapping[str, Any]], bool] | None = None,
        thread_lookup: Callable[[str], object] | None = None,
        group_notes: Any | None = None,
        knowledge_selector: Any | None = None,
        knowledge_context_supplier: Callable[[ParticipationOpportunity, Mapping[str, Any]], Any]
        | None = None,
        trend_rows: Callable[[str, str, str], Mapping[str, Any] | None] | None = None,
    ) -> None:
        # Kept on the constructor for old object construction; decision inputs are the
        # sole source of trusted policy values during ``build``.
        self._policy = policy
        self._anchors = anchors
        self._clock = clock
        self._source_authorizer = source_authorizer
        # Registry lookup by provider message id; referenced, never changed.
        self._thread_lookup = thread_lookup
        # Weekly group note reader (``GroupNotesReader``); same chat only, by mode.
        self._group_notes = group_notes
        # Same-chat Knowledge for trigger authors proven on the current roster; both or
        # neither, and absent means the judge and writer see no Knowledge at all.
        self._knowledge_selector = knowledge_selector
        self._knowledge_context_supplier = knowledge_context_supplier
        # Offered trend cards (``trend:`` ids) are owner-approved material, not archive
        # rows; without a reader such a source is unavailable like any missing row.
        self._trend_rows = trend_rows
        self._fallback_logged_ms: dict[tuple[str, str], int] = {}

    @property
    def _archive(self):
        from yeoman_gateway.adapters.reply_archive_history import HistoryReplyArchiveAdapter
        from yeoman_gateway.history.context import current_history_snapshot
        from yeoman_gateway.history.queries import HistoryQueries
        snapshot = current_history_snapshot()
        if snapshot is None:
            raise HistoryPaused('history_scope_required')
        return _ParticipationArchive(HistoryReplyArchiveAdapter(HistoryQueries(snapshot)))

    async def build(
        self,
        opportunity: ParticipationOpportunity,
        *,
        inputs: ParticipationDecisionInputs,
        now_ms: int | None = None,
    ) -> dict[str, object]:
        """The bounded context for one opportunity.

        ``messages`` holds same-chat retained sources including intervening
        participants, ``anchors`` holds delivered Arvid text only, and the remaining
        keys are the trusted decision inputs (allowed actions, remaining budgets,
        guidance, revisions). Truncation is reported instead of hidden.
        """
        limits = inputs.bounds
        moment = int(now_ms if now_ms is not None else _now_ms())
        since = datetime.fromtimestamp(
            (moment - int(limits.window_minutes) * 60_000) / 1000, UTC
        )
        until = datetime.fromtimestamp(moment / 1000, UTC)
        since_ms = int(since.timestamp()) * 1000
        archive = self._archive
        rows = archive.lookup_messages_in_range(
            opportunity.channel,
            opportunity.chat_id,
            since,
            until,
            limit=max(1, min(int(limits.max_messages) * 4, 300)),
            latest=True,
        )
        range_rows = [row for row in rows if isinstance(row, Mapping)]

        # Resolve every required source by its exact archive key. The bounded history
        # query is only an optional-context optimization and cannot hide a trigger.
        required_ids = _unique_ids(opportunity.source_event_ids)
        required_rows: dict[str, dict[str, object]] = {}
        trend_ids: set[str] = set()
        for source_id in required_ids:
            if is_trend_source(source_id):
                trend_row = (
                    self._trend_rows(opportunity.channel, opportunity.chat_id, source_id)
                    if self._trend_rows is not None
                    else None
                )
                if not isinstance(trend_row, Mapping) or _row_id(trend_row) != source_id:
                    raise ParticipationDecisionError("source_unavailable", detail=source_id)
                required_rows[source_id] = dict(trend_row)
                trend_ids.add(source_id)
                continue
            row = archive.lookup_message(
                opportunity.channel, opportunity.chat_id, source_id
            )
            if not isinstance(row, Mapping) or _row_id(row) != source_id:
                raise ParticipationDecisionError("source_unavailable", detail=source_id)
            rejection = self._source_rejection(
                row,
                source_id=source_id,
                opportunity=opportunity,
                since_ms=since_ms,
                until_ms=moment,
                snapshot=inputs.snapshot,
            )
            if rejection:
                raise ParticipationDecisionError(rejection, detail=source_id)
            required_rows[source_id] = dict(row)

        optional: list[dict[str, object]] = []
        seen_optional: set[str] = set(required_rows)
        for row in range_rows:
            source_id = _row_id(row)
            if not source_id or source_id in seen_optional:
                continue
            if str(row.get("channel") or "") != str(opportunity.channel):
                continue
            if str(row.get("chat_id") or "") != str(opportunity.chat_id):
                continue
            if self._source_rejection(
                row,
                source_id=source_id,
                opportunity=opportunity,
                since_ms=since_ms,
                until_ms=moment,
                snapshot=inputs.snapshot,
            ):
                # Optional sources are data, not authority: remove them before prompt
                # construction, while required-source failures remain hard errors.
                continue
            optional.append(dict(row))
            seen_optional.add(source_id)

        ordered = sorted(
            [*required_rows.values(), *optional], key=_row_sort_key
        )
        required = [row for row in ordered if _row_id(row) in set(required_ids)]
        optional = [row for row in ordered if _row_id(row) not in set(required_ids)]
        max_messages = max(0, int(limits.max_messages))
        if len(required) > max_messages:
            raise ParticipationDecisionError("context_too_large", detail="required_sources_exceed_message_limit")
        kept_required = required
        free = max(0, max_messages - len(kept_required))
        conversation: Conversation | None = None
        earlier: list[dict[str, object]] = []
        earlier_ids: list[str] = []
        chat_required = [row for row in required if _row_id(row) not in trend_ids]
        if limits.context_mode == "conversation" and chat_required:
            try:
                conversation = self._conversation(
                    archive, opportunity, range_rows, chat_required, limits, now_ms=moment
                )
            except (HistoryPaused, ParticipationDecisionError):
                raise
            except Exception as exc:  # noqa: BLE001 - fall back to today's window
                self._log_fallback(opportunity, exc, now_ms=moment)
        if conversation is None:
            selected = kept_required + (optional[-free:] if free else [])
        else:
            members = set(conversation.message_ids)
            in_conversation = [row for row in optional if _history_id(row) in members]
            first_kept = bool(in_conversation) and any(
                _history_id(row) in members
                and _row_sort_key(row) < _row_sort_key(in_conversation[0])
                for row in kept_required
            )
            kept_conversation = _cap_conversation(in_conversation, free, first_kept=first_kept)
            earlier = _earlier_context(
                optional,
                members,
                start_ms=conversation.start_ms,
                visible=len(kept_conversation)
                + sum(_history_id(row) in members for row in kept_required),
                floor=max(0, int(limits.conversation_min_messages)),
                free=free - len(kept_conversation),
            )
            selected = kept_required + kept_conversation + earlier
            if earlier:
                # Everything shown from before the conversation start sits above the
                # separator, also a required source from there.
                earlier_ids = [
                    _row_id(row) for row in sorted(selected, key=_row_sort_key)
                    if _history_id(row) not in members
                    and (_history_shape(row)["sent_ms"] or 0) < conversation.start_ms
                ]
        selected.sort(key=_row_sort_key)
        selected_ids = {_row_id(row) for row in selected}
        dropped_ids = [
            _row_id(row)
            for row in ordered
            if _row_id(row) and _row_id(row) not in selected_ids
        ]
        truncated = len(ordered) - len(selected)

        messages = [_render_message(row) for row in selected if _row_id(row)]
        trend_posts = {
            source_id: str(required_rows[source_id].get("trend_post") or "")
            for source_id in sorted(trend_ids)
        }
        anchors: list[dict[str, object]] = []
        if self._anchors is not None:
            raw_anchors = await self._anchors.delivered_anchors(
                opportunity.channel,
                opportunity.chat_id,
                since_ms=since_ms,
                limit=int(limits.max_anchors),
            )
            for anchor in raw_anchors:
                if not isinstance(anchor, Mapping):
                    continue
                if str(anchor.get("delivery_state") or "") != "delivered":
                    # Only recipient evidence creates a statement of Arvid's.
                    continue
                if not anchor.get("provider_message_id"):
                    continue
                current = dict(anchor)
                if opportunity.channel == 'whatsapp':
                    row = self._archive.lookup_message(opportunity.channel, opportunity.chat_id,
                        str(anchor['provider_message_id']))
                    if row is None:
                        continue
                    current['message'] = row['text']
                anchors.append(current)

        current_source_ids = _unique_ids(inputs.current_source_ids) or required_ids
        allowed_intents = tuple(sorted(str(item) for item in inputs.allowed_intents))
        budgets = dict(inputs.remaining_budgets)
        context: dict[str, object] = {
            "channel": opportunity.channel,
            "chat_id": opportunity.chat_id,
            "opportunity_id": opportunity.opportunity_id,
            "trigger": opportunity.trigger,
            "lane": opportunity.lane,
            "messages": messages,
            "anchors": anchors,
            "allowed_actions": list(inputs.allowed_actions),
            "allowed_intents": list(allowed_intents),
            "allowed_contribution_types": list(
                _snapshot_value(inputs.snapshot, "allowed_contribution_types", ())
            ),
            "budgets": budgets,
            "remaining_budgets": budgets,
            "reservation_limits_by_intent": _render_reservation_limits(
                inputs.reservation_limits_by_intent
            ),
            "approval_required": bool(inputs.approval_required),
            "arbitration_revision": int(inputs.arbitration_revision),
            "continuation_candidate": bool(inputs.continuation_candidate),
            "current_source_ids": list(current_source_ids),
            "source_event_ids": list(current_source_ids),
            "guidance": _snapshot_guidance(inputs.snapshot),
            "policy_revision": _snapshot_policy_revision(inputs.snapshot),
            "context_revision": int(opportunity.observed_revision),
            "truncated_messages": truncated,
            "dropped_source_ids": dropped_ids[:32],
            "dropped_source_count": len(dropped_ids),
            "context_mode": "conversation" if conversation is not None else "window",
            "conversation_id": conversation.id if conversation is not None else "",
            "conversation_header": render_header(conversation) if conversation else "",
            "conversation": (
                {**conversation.stats(), "earlier_messages": len(earlier)}
                if conversation is not None
                else None
            ),
            # Shown messages from before the conversation start (only when the floor
            # added earlier context); the Judge view draws one separator after them.
            "earlier_message_ids": earlier_ids,
            "direct_addressed": bool(_snapshot_value(inputs.snapshot, "direct_addressed", False)),
            "allows_continuation": "continue" in inputs.allowed_intents,
            # Fixed text of each offered trend card; a card is posted verbatim.
            "trend_posts": trend_posts,
        }
        selection_started = time.monotonic()
        context["knowledge_selection_status"] = "disabled"
        context["knowledge_selected_count"] = 0
        context["knowledge_rendered_chars"] = 0
        if (
            self._knowledge_selector is not None
            and self._knowledge_context_supplier is not None
            and not (trend_ids and trend_ids == set(required_ids))
        ):
            try:
                trusted = self._knowledge_context_supplier(opportunity, context)
                readers = _knowledge_readers(trusted)
                if isinstance(trusted, str):
                    context["knowledge_selection_status"] = trusted
                elif readers is not None:
                    query = _knowledge_query(context, current_source_ids)
                    selection = self._knowledge_selector.select_for_readers(
                        query=query,
                        readers=readers,
                    )
                    status = str(getattr(selection, "reason", "error"))
                    context["knowledge_selection_status"] = (
                        "no_hit" if status == "empty" and query else
                        "empty" if status == "empty" else status
                    )
                    if selection.text:
                        facts = selection.facts
                        context["knowledge_selected_count"] = (
                            len(selection.statements.statement_ids)
                            + len(facts.used_source_refs)
                        )
                        context["knowledge_rendered_chars"] = len(selection.text)
                    if selection.text:
                        context["selected_knowledge_text"] = selection.text
                    # Private controller handles: renderers consume only the text above.
                    context["_knowledge_selection"] = selection
                    context["_knowledge_readers"] = readers
                    # Encode the bounded, versioned evidence here, where the real
                    # selection exists, so the approval path can persist exactly what
                    # influenced the draft. Undecodable evidence is simply absent.
                    context["_knowledge_evidence"] = _encoded_knowledge_evidence(
                        selection
                    )
                else:
                    context["knowledge_selection_status"] = "denied"
            except HistoryPaused:
                raise
            except Exception:
                # Retrieval outages preserve the existing recent-chat-only path.
                context["knowledge_selection_status"] = "error"
        context["knowledge_selection_elapsed_ms"] = int(
            (time.monotonic() - selection_started) * 1000
        )
        self._attach_group_note(context, opportunity, now_ms=moment)
        return context

    def _attach_group_note(
        self,
        context: dict[str, object],
        opportunity: ParticipationOpportunity,
        *,
        now_ms: int,
    ) -> None:
        """Add this chat's weekly note: rendered when ``on``, held aside in ``shadow``.

        ``off`` (and no reader) leaves the context exactly as it was. In ``shadow`` the
        note sits under a private key that neither the Judge nor the Writer renders;
        only the paired shadow Judge call reads it.
        """
        from yeoman_gateway.processing.participation_group_notes import SHADOW_NOTE_KEY

        if self._group_notes is None:
            return
        mode = self._group_notes.mode()
        if mode not in ("on", "shadow"):
            return
        note = self._group_notes.note_for(
            opportunity.channel, opportunity.chat_id, now_ms=now_ms
        )
        if note is None:
            return
        if mode == "on":
            context["group_notes_text"] = note.text
            context["group_notes_week"] = note.week
            context["group_notes_digest_id"] = note.digest_id
        else:
            context[SHADOW_NOTE_KEY] = note.as_mapping()

    # -- conversation view ---------------------------------------------------------------

    def _conversation(
        self,
        archive: Any,
        opportunity: ParticipationOpportunity,
        range_rows: list[Mapping[str, Any]],
        required: list[dict[str, object]],
        limits: ParticipationContextBounds,
        *,
        now_ms: int,
    ) -> Conversation:
        """The conversation holding the newest required source, from the window's rows.

        Every same-chat row read for the window is segmented (also rows the judge will
        not see, such as Arvid's own messages), so boundaries follow the real chat.
        """
        rows: dict[str, dict[str, Any]] = {}
        for row in [*range_rows, *required]:
            if str(row.get("channel") or "") != str(opportunity.channel):
                continue
            if str(row.get("chat_id") or "") != str(opportunity.chat_id):
                continue
            shaped = _history_shape(row)
            if shaped["message_id"]:
                rows.setdefault(str(shaped["message_id"]), shaped)
        queries = getattr(getattr(archive, "adapter", None), "queries", None)
        reactions = (
            queries.reactions(message_ids=tuple(rows)[-500:], until_ms=now_ms)
            if queries is not None and rows
            else []
        )
        conversations = segment(
            list(rows.values()),
            reactions,
            lull_ms=int(limits.lull_minutes) * 60_000,
            burst_n=int(limits.burst_messages),
            burst_ms=int(limits.burst_minutes) * 60_000,
            now_ms=now_ms,
            registry_ids=registry_ids(rows.values(), self._thread_lookup),
        )
        if not conversations:
            raise ValueError("no conversation in the window")
        if not required:
            return conversations[-1]
        newest = _history_id(max(required, key=_row_sort_key))
        for conversation in conversations:
            if newest in conversation.message_ids:
                return conversation
        raise ValueError("newest required source is in no conversation")

    def _log_fallback(
        self, opportunity: ParticipationOpportunity, exc: Exception, *, now_ms: int
    ) -> None:
        key = (str(opportunity.channel), str(opportunity.chat_id))
        last = self._fallback_logged_ms.get(key)
        if last is not None and now_ms - last < _FALLBACK_LOG_INTERVAL_MS:
            return
        self._fallback_logged_ms[key] = now_ms
        logger.warning(
            "participation_conversation_fallback chat={} error_type={}",
            str(opportunity.chat_id)[:24],
            type(exc).__name__,
        )

    # -- source trust ------------------------------------------------------------------

    def _source_rejection(
        self,
        row: Mapping[str, Any],
        *,
        source_id: str,
        opportunity: ParticipationOpportunity,
        since_ms: int,
        until_ms: int,
        snapshot: Any,
    ) -> str | None:
        if str(row.get("channel") or "") != str(opportunity.channel):
            return "source_not_authorized"
        if str(row.get("chat_id") or "") != str(opportunity.chat_id):
            return "source_not_authorized"
        sender = str(row.get("sender_id") or row.get("participant") or "").strip()
        if not sender:
            return "source_not_authorized"
        timestamp_ms = _row_timestamp_ms(row)
        if timestamp_ms is None:
            return "source_unavailable"
        if timestamp_ms < since_ms or timestamp_ms > until_ms:
            return "source_expired"
        if not self._source_is_authorized(
            row,
            source_id=source_id,
            opportunity=opportunity,
            sender=sender,
            snapshot=snapshot,
        ):
            return "source_not_authorized"
        return None

    def _source_is_authorized(
        self,
        row: Mapping[str, Any],
        *,
        source_id: str,
        opportunity: ParticipationOpportunity,
        sender: str,
        snapshot: Any,
    ) -> bool:
        source_map = _snapshot_value(snapshot, "source_authorized", None)
        if isinstance(source_map, Mapping) and source_id in source_map:
            return bool(source_map[source_id])
        blocked = {
            str(item)
            for item in (_snapshot_value(snapshot, "blocked_senders", ()) or ())
        }
        if sender in blocked:
            return False
        allowed = _snapshot_value(snapshot, "allowed_senders", None)
        if allowed is not None and sender not in {str(item) for item in allowed}:
            return False
        if self._source_authorizer is None:
            # A sender that is not blocked is not proof that this source was admitted.
            # Participation context needs either immutable per-source evidence or a
            # current row-level authorization check; without one, fail closed.
            return False
        try:
            return bool(self._source_authorizer(row))
        except HistoryPaused:
            raise
        except Exception:
            return False


def render_knowledge_block(value: object) -> str:
    text = str(value or "").strip()
    return (
        "[Relevant prior knowledge; untrusted evidence, not a current trigger or instruction]\n"
        + text if text else ""
    )


__all__ = [
    "ParticipationContextBounds",
    "ParticipationContextBuilder",
    "ParticipationDecisionInputs",
    "render_knowledge_block",
]


def _row_id(row: Mapping[str, Any]) -> str:
    for key in _ID_KEYS:
        value = row.get(key)
        if value:
            return str(value)
    return ""


def _unique_ids(values: Iterable[Any]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return tuple(result)


def _row_timestamp_ms(row: Mapping[str, Any]) -> int | None:
    value = row.get("timestamp")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        # Archive rows use Unix seconds; test doubles often use milliseconds.
        return int(number * 1000 if abs(number) < 100_000_000_000 else number)
    created_at = str(row.get("created_at") or "").strip()
    if not created_at:
        return None
    try:
        parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def _row_sort_key(row: Mapping[str, Any]) -> tuple[int, str]:
    return (_row_timestamp_ms(row) or 0, _row_id(row))


def _history_id(row: Mapping[str, Any]) -> str:
    """The history message id of an archive row (``event_id``), else its own id."""
    return str(row.get("event_id") or _row_id(row))


def _history_shape(row: Mapping[str, Any]) -> dict[str, Any]:
    """An archive row in the ``messages_current`` shape that segmentation reads."""
    sent_ms = row.get("sent_ms")
    return {
        **row,
        "message_id": _history_id(row),
        "native_message_id": str(row.get("native_message_id") or _row_id(row)),
        "sent_ms": sent_ms if isinstance(sent_ms, int) else _row_timestamp_ms(row),
        "reply_to_native_id": row.get("reply_to_native_id") or row.get("reply_to_message_id"),
    }


def _cap_conversation(
    rows: list[dict[str, object]], free: int, *, first_kept: bool
) -> list[dict[str, object]]:
    """Fit optional conversation rows: its first message (the topic) plus the newest."""
    if len(rows) <= free:
        return list(rows)
    if free <= 0:
        return []
    if first_kept:
        return rows[-free:]
    return [rows[0], *(rows[-(free - 1):] if free > 1 else [])]


def _earlier_context(
    optional: list[dict[str, object]],
    members: set[str],
    *,
    start_ms: int,
    visible: int,
    floor: int,
    free: int,
) -> list[dict[str, object]]:
    """The newest optional rows before the conversation, up to the floor and the cap.

    ``optional`` is in time order and already a subset of the window's rows, so the
    result is too; required sources are never part of it.
    """
    wanted = min(floor - visible, free)
    if wanted <= 0:
        return []
    before = [
        row for row in optional
        if _history_id(row) not in members
        and isinstance(sent := _history_shape(row)["sent_ms"], int)
        and sent < start_ms
    ]
    return before[-wanted:]


def _freeze_budget_pairs(value: Any) -> tuple[tuple[str, int], ...]:
    entries = value.items() if isinstance(value, Mapping) else (value or ())
    result: list[tuple[str, int]] = []
    for entry in entries:
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            continue
        result.append((str(entry[0]), int(entry[1])))
    return tuple(result)


def _validate_reservation_limits(
    value: Any,
) -> tuple[
    tuple[str, tuple[tuple[str, int, int] | tuple[str, int, int, str], ...]], ...
]:
    """Validate the immutable tuple shape accepted by the participation ledger."""
    field = "reservation_limits_by_intent"
    if not isinstance(value, tuple):
        raise TypeError(f"{field} must be a tuple")
    for group in value:
        if (
            not isinstance(group, tuple)
            or len(group) != 2
            or not isinstance(group[0], str)
            or not isinstance(group[1], tuple)
        ):
            raise TypeError(f"{field} entries must be (intent, tuple[limits, ...])")
        for limit in group[1]:
            if not isinstance(limit, tuple) or len(limit) not in (3, 4):
                raise TypeError(f"{field} limit entries must have 3 or 4 tuple items")
            if not isinstance(limit[0], str) or any(
                not isinstance(item, int) or isinstance(item, bool) for item in limit[1:3]
            ):
                raise TypeError(f"{field} limit entries need (category, int, int[, kind])")
            if len(limit) == 4 and not isinstance(limit[3], str):
                raise TypeError(f"{field} limit window kind must be a string")
    return value


def _snapshot_value(snapshot: Any, name: str, default: Any = None) -> Any:
    if isinstance(snapshot, Mapping):
        return snapshot.get(name, default)
    return getattr(snapshot, name, default)


def _snapshot_participation(snapshot: Any) -> Any:
    return _snapshot_value(snapshot, "participation", None)


def _snapshot_guidance(snapshot: Any) -> str:
    direct = _snapshot_value(snapshot, "guidance", None)
    value = direct if direct is not None else _snapshot_value(_snapshot_participation(snapshot), "guidance", "")
    return str(value or "")


def _snapshot_policy_revision(snapshot: Any) -> str:
    for name in ("policy_version", "version", "policy_hash"):
        value = _snapshot_value(snapshot, name, "")
        if value:
            return str(value)
    return ""


def _render_reservation_limits(value: Any) -> dict[str, list[list[Any]]]:
    result: dict[str, list[list[Any]]] = {}
    for intent, limits in value:
        result[intent] = [list(limit) for limit in limits]
    return result


def _render_message(row: Mapping[str, Any]) -> dict[str, object]:
    """One context message. Media summaries are reused, never invented."""
    text = str(row.get("text") or "").strip()
    media_summary = ""
    if "[Derived description]:" in text:
        text, _, description = text.partition("[Derived description]:")
        media_summary = "[Derived description]:" + description
        text = text.strip()
    elif "[image_description]" in text:
        media_summary = text
        text = ""
    return {
        "event_id": _row_id(row),
        "message_id": _row_id(row),
        "sender": str(row.get("sender_name") or row.get("sender_id") or row.get("participant") or "?"),
        "sender_id": str(row.get("sender_id") or row.get("participant") or ""),
        "text": text,
        "media_summary": media_summary,
        "timestamp": row.get("timestamp"),
        "channel": str(row.get("channel") or ""),
        "chat_id": str(row.get("chat_id") or ""),
        "reply_to_message_id": str(row.get("reply_to_message_id") or ""),
    }


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)


def _encoded_knowledge_evidence(selection: object) -> dict[str, Any] | None:
    """Encode a selection as durable evidence, or ``None`` if it cannot be encoded.

    A selection produced by anything other than the real selector (a test double, for
    instance) has no encodable identity, so it yields no evidence rather than a
    half-written snapshot.
    """
    from yeoman_gateway.processing.participation_knowledge import (
        ParticipationKnowledgeEvidenceError,
        selection_to_mapping,
    )

    try:
        return selection_to_mapping(selection)
    except ParticipationKnowledgeEvidenceError:
        return None
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def _knowledge_readers(trusted: object) -> ParticipationKnowledgeReaders | None:
    """Accept the verified reader bundle, and a bare single reader pair for tests."""
    if isinstance(trusted, ParticipationKnowledgeReaders):
        return trusted
    if isinstance(trusted, tuple) and len(trusted) == 2:
        return readers_from_single(trusted[0], trusted[1])
    return None


def _bounded_query_prefix(entry: str, budget: int) -> str:
    """Return at most ``budget`` characters from the start of ``entry``.

    A trigger that cannot fit whole still has to contribute something: dropping it
    would leave an otherwise authorized long trigger with no query at all. The whole
    ``budget`` is always spent. The only shaping is dropping a trailing partial word:
    when the prefix ends inside a word whose completed length also fits the budget,
    that split word is removed rather than counted as a term, because a lexically
    meaningful boundary is worth more to the selector than a truncated token.
    """
    prefix = entry[:budget]
    clipped = prefix.rstrip(" ")
    tail_start = clipped.rfind(" ") + 1
    if tail_start > 0 and len(clipped) - tail_start <= budget:
        return clipped
    return prefix


def _knowledge_query(context: Mapping[str, Any], source_ids: tuple[str, ...]) -> str:
    rows = context.get("messages")
    if not isinstance(rows, list):
        return ""
    source_set = set(source_ids)
    required: list[str] = []
    nearby: list[str] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        text = str(row.get("text") or row.get("media_summary") or "").strip()
        if not text:
            continue
        message_id = _row_id(row)
        normalized = " ".join(text.split())
        if message_id in source_set:
            required.append(normalized)
        else:
            nearby.append(normalized)
    result: list[str] = []
    used = 0
    for entry in (*required, *nearby[-3:]):
        separator = 1 if result else 0
        budget = 600 - used - separator
        if budget <= 0:
            break
        if len(entry) > budget:
            entry = _bounded_query_prefix(entry, budget)
            if not entry:
                break
        result.append(entry)
        used += separator + len(entry)
    return " ".join(result)


__all__ = [
    "ParticipationContextBounds",
    "ParticipationContextBuilder",
    "ParticipationDecisionInputs",
]
