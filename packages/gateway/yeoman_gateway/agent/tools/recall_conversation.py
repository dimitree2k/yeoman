"""Tool for searching session conversation history on demand."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable

from yeoman_gateway.agent.tools.base import Tool

if TYPE_CHECKING:
    from yeoman_gateway.people.resolver import PeopleResolver
    from yeoman_gateway.session.manager import SessionManager


_RELATIVE_PERIOD = re.compile(r"^(?:last_)?(\d{1,3})([hd])$")
_PERIOD_HELP = (
    "Error: period must be 'today', 'yesterday', a date like '2026-10-18', "
    "or a span like '6h' or '3d'."
)


def period_bounds(period: str, now_ms: int) -> tuple[int, int] | None:
    """Return the local-time window ``[start_ms, end_ms)`` that ``period`` names.

    Days follow the gateway's local timezone (DST included), the same clock the
    prompt shows as "Current local datetime", so "today" means the owner's today.
    """
    value = period.strip().lower()
    now = datetime.fromtimestamp(now_ms / 1000)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    relative = _RELATIVE_PERIOD.match(value)
    if value == "today":
        start, end = midnight, now
    elif value == "yesterday":
        start, end = midnight - timedelta(days=1), midnight
    elif relative:
        amount = int(relative[1])
        if amount < 1:
            return None
        span = timedelta(hours=amount) if relative[2] == "h" else timedelta(days=amount)
        start, end = now - span, now
    else:
        try:
            day = date.fromisoformat(value)
        except ValueError:
            return None
        start = datetime(day.year, day.month, day.day)
        end = start + timedelta(days=1)
    # The window's end is exclusive; "today" and relative spans include this moment.
    end_ms = now_ms + 1 if end is now else int(end.timestamp() * 1000)
    return int(start.timestamp() * 1000), end_ms


def _local_time(sent_ms: Any) -> str:
    if not isinstance(sent_ms, int):
        return "time unknown"
    return datetime.fromtimestamp(sent_ms / 1000).strftime("%Y-%m-%d %H:%M")


class RecallConversationTool(Tool):
    """Search conversation history for messages matching a query."""

    def __init__(
        self,
        session_manager: "SessionManager",
        group_resolver: Callable[[str], tuple[str | None, str | None]] | None = None,
        *,
        people: PeopleResolver | None = None,
    ) -> None:
        self._session_manager = session_manager
        self._group_resolver = group_resolver
        self._people = people
        self._channel = ""
        self._chat_id = ""
        self._is_owner = False

    def set_context(self, channel: str, chat_id: str, *, is_owner: bool = False) -> None:
        self._channel = channel
        self._chat_id = chat_id
        self._is_owner = is_owner

    @property
    def _is_owner(self) -> bool:
        from yeoman_gateway.processing.tool_context import current_tool_context

        context = current_tool_context()
        return context.is_owner if context is not None else self._default_is_owner

    @_is_owner.setter
    def _is_owner(self, value: bool) -> None:
        self._default_is_owner = value

    @property
    def _channel(self) -> str:
        from yeoman_gateway.processing.tool_context import tool_target

        return tool_target(
            default_channel=self._default_channel, default_chat_id=self._default_chat_id
        )[0]

    @_channel.setter
    def _channel(self, value: str) -> None:
        self._default_channel = value

    @property
    def _chat_id(self) -> str:
        from yeoman_gateway.processing.tool_context import tool_target

        return tool_target(
            default_channel=self._default_channel, default_chat_id=self._default_chat_id
        )[1]

    @_chat_id.setter
    def _chat_id(self, value: str) -> None:
        self._default_chat_id = value

    @property
    def name(self) -> str:
        return "recall_conversation"

    @property
    def description(self) -> str:
        return (
            "Search earlier conversation history for messages matching a query. "
            "Use when the user references something from a previous part of the "
            "conversation that is not in your visible context. "
            "Searches across session boundaries (including before /new). "
            "For an explicit owner question in the owner's WhatsApp DM, use person "
            "for questions such as 'what did Alex say?' or 'what does Alex hold?', "
            "or group/chat for a named group. These parameters search only chats "
            "whose current membership includes the owner and are refused elsewhere. "
            "Person names can be partial; if multiple people match, use a fuller name. "
            "For questions about a time span ('what happened today in the group?', "
            "'how did people react to my messages yesterday?'), set period and leave "
            "query out: that lists the messages of that span in order, including your "
            "own (role assistant), with their current emoji reactions. A query searches "
            "keywords only and misses messages that do not contain the words."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Keywords to match against message content. Optional when "
                        "period is set; then the whole span is listed."
                    ),
                },
                "period": {
                    "type": "string",
                    "description": (
                        "Optional time span in local time: 'today', 'yesterday', a date "
                        "('2026-10-18'), or the last hours/days ('6h', '3d')."
                    ),
                },
                "max_messages": {
                    "type": "integer",
                    "description": "Maximum number of matching messages to return.",
                    "minimum": 1,
                    "maximum": 50,
                },
                "group": {
                    "type": "string",
                    "description": "Optional group name to search from the owner’s direct chat.",
                },
                "chat": {
                    "type": "string",
                    "description": "Alias for group: an optional group name to search from the owner’s direct chat.",
                },
                "person": {
                    "type": "string",
                    "description": "Optional exact person name; available only in the owner’s direct chat.",
                },
            },
        }

    async def execute(self, **kwargs: Any) -> str:
        if not self._channel or not self._chat_id:
            return "Error: no chat context set."

        query = str(kwargs.get("query") or "").strip().lower()
        period = str(kwargs.get("period") or "").strip()
        if not query and not period:
            return "Error: supply a query, a period, or both."

        max_messages = int(kwargs.get("max_messages") or 30)
        max_messages = max(1, min(50, max_messages))
        now_ms = __import__("time").time_ns() // 1_000_000
        bounds = period_bounds(period, now_ms) if period else None
        if period and bounds is None:
            return _PERIOD_HELP
        # Query helpers take exclusive bounds on both ends.
        after_ms = bounds[0] - 1 if bounds else None
        before_ms = bounds[1] if bounds else None

        from yeoman_gateway.processing.tool_context import (
            current_tool_context,
            tool_history_snapshot,
        )

        context = current_tool_context()
        person = str(kwargs.get("person") or "").strip()
        group = str(kwargs.get("group") or "").strip()
        chat = str(kwargs.get("chat") or "").strip()
        if group and chat:
            return "Error: supply either 'group' or 'chat', not both."
        target_ref = group or chat
        if person or target_ref:
            from yeoman_gateway.history.queries import HistoryQueries

            if not self._is_owner or self._channel != "whatsapp":
                return "Person and cross-chat history lookup is available only in the owner's direct chat."
            if context is None or context.history_snapshot is None:
                return "Owner history lookup is unavailable in this context."
            principal = context.canonical_user_id
            if not HistoryQueries(context.history_snapshot).is_owner_direct_chat(
                principal, self._chat_id, now_ms, is_owner=self._is_owner, channel=self._channel
            ):
                return "Person and cross-chat history lookup is available only in the owner's direct chat."
        snapshot = tool_history_snapshot()
        if self._channel == "whatsapp":
            from yeoman_gateway.adapters.reply_archive_history import history_text
            from yeoman_gateway.history.queries import HistoryQueries

            queries = HistoryQueries(snapshot)
            if person or target_ref:
                principal = context.canonical_user_id if context is not None else ""
                member_chats = queries.owner_member_chat_ids(principal, now_ms)
                if target_ref:
                    if self._group_resolver is None:
                        return "Error: group lookup is unavailable."
                    target, error = self._group_resolver(target_ref)
                    if error or not target:
                        return str(error or "Error: group not found.")
                    if target not in member_chats:
                        return "That group is not in your current WhatsApp memberships."
                    chat_ids = (target,)
                else:
                    chat_ids = member_chats
                if person:
                    from yeoman_gateway.people.bridges import history_person
                    from yeoman_gateway.people.disclosure import reader_context, render_person
                    from yeoman_gateway.people.resolver import PeopleUnavailable, resolve_person

                    if self._people is None:
                        return "Person lookup is unavailable."
                    try:
                        reader = reader_context(
                            self._people,
                            channel=self._channel,
                            chat_id=self._chat_id,
                            principal_id=principal,
                            is_owner=self._is_owner,
                            at_ms=now_ms,
                            history=snapshot,
                        )
                        resolution = resolve_person(person, reader)
                        if resolution.status == "ambiguous":
                            cards = [
                                render_person(candidate.person_id, reader)
                                for candidate in resolution.candidates[:8]
                            ]
                            candidates = "; ".join(
                                f"{card.display_name} (groups: {', '.join(card.shared_chat_names)})"
                                for card in cards
                            )
                            truncated = (
                                " (candidate list truncated)"
                                if len(resolution.candidates) > 8
                                else ""
                            )
                            return (
                                f"Several people match '{person}': {candidates}{truncated}. "
                                "Please re-call with a fuller name."
                            )
                        if resolution.status != "resolved" or resolution.person_id is None:
                            return f"No person matching '{person}' was found in those chats."
                        card = render_person(resolution.person_id, reader)
                        target = history_person(resolution.person_id, reader)
                        rows = queries.search_person(
                            chat_ids=chat_ids,
                            person=target,
                            at_ms=min(now_ms, before_ms - 1) if before_ms else now_ms,
                            limit=max_messages,
                            query=query,
                            after_ms=after_ms,
                        )
                    except PeopleUnavailable:
                        return "Person lookup is unavailable."
                else:
                    rows = self._read(queries, chat_ids, query, max_messages, after_ms, before_ms)
            else:
                rows = self._read(
                    queries, (self._chat_id,), query, max_messages, after_ms, before_ms
                )
            if not rows:
                if not query:
                    return f"No messages found for period '{period}'."
                scope = f" in period '{period}'" if period else ""
                return f"No matching messages found for '{query}'{scope}."
            query_warning = (
                f"No messages matched the query terms '{query}'.\n"
                if person and query and all(row["query_match_count"] == 0 for row in rows)
                else ""
            )
            from yeoman_gateway.history.conversations import current_reactions

            reactions = current_reactions(
                queries.reactions(
                    message_ids=tuple(str(row["message_id"]) for row in rows), until_ms=now_ms
                )
            )
            author_names = {}
            if person or target_ref:
                for contact in {
                    str(row["sender_contact_id"])
                    for row in rows
                    if row["sender_contact_id"]
                    and row["sender_basis"] in ("native_identifier", "owner_attested")
                }:
                    author = queries.contact(contact)
                    author_names[contact] = author.get("display_name") if author else None
            lines = []
            for row in rows:
                role = "assistant" if row["direction"] == "out" else "user"
                attribution = ""
                if person or target_ref:
                    if person and row.get("person_authored"):
                        name = card.display_name
                    elif row["sender_contact_id"] and row["sender_basis"] in (
                        "native_identifier",
                        "owner_attested",
                    ):
                        name = (
                            author_names.get(str(row["sender_contact_id"]))
                            or row["sender_identifier"]
                            or "unverified"
                        )
                    else:
                        name = "unverified"
                    attribution = f" [chat={row['chat_id']}; sender={name}]"
                emojis = reactions.get(str(row["message_id"]))
                reacted = f" [reactions: {' '.join(emojis)}]" if emojis else ""
                lines.append(
                    f"[{_local_time(row['sent_ms'])}] [{role}]{attribution}: "
                    f"{history_text(row)}{reacted}"
                )
            if query:
                header = f"Found {len(lines)} matching message(s):\n"
            else:
                header = f"Found {len(lines)} message(s) for period '{period}', oldest first:\n"
                if len(lines) >= max_messages:
                    header += (
                        f"Showing only the latest {max_messages}; earlier messages of this "
                        "period were left out.\n"
                    )
            return query_warning + header + "\n".join(lines)

        if not query or period:
            return "Error: period is available only for WhatsApp history; use a query here."

        session_key = f"{self._channel}:{self._chat_id}"
        session = self._session_manager.get_or_create(session_key)

        # Search ALL messages (ignoring boundaries) for query matches.
        matches: list[str] = []
        for msg in session.messages:
            role = str(msg.get("role") or "")
            if role not in ("user", "assistant"):
                continue
            content = str(msg.get("content") or "")
            if query in content.lower():
                ts = msg.get("timestamp", "?")
                matches.append(f"[{ts}] [{role}]: {content[:300]}")
                if len(matches) >= max_messages:
                    break

        if not matches:
            return f"No matching messages found for '{query}'."

        header = f"Found {len(matches)} matching message(s):\n"
        return header + "\n".join(matches)

    @staticmethod
    def _read(
        queries: Any,
        chat_ids: tuple[str, ...],
        query: str,
        limit: int,
        after_ms: int | None,
        before_ms: int | None,
    ) -> list[Any]:
        """Keyword search, or, without a query, one chat's latest messages of the period."""
        if query:
            return queries.search(
                chat_ids=chat_ids, query=query, limit=limit, after_ms=after_ms, before_ms=before_ms
            )
        rows: list[Any] = []
        for chat_id in chat_ids:
            rows.extend(
                queries.recent(
                    chat_id=chat_id,
                    limit=limit,
                    after_ms=after_ms,
                    until_ms=before_ms - 1 if before_ms is not None else None,
                )
            )
        return rows
