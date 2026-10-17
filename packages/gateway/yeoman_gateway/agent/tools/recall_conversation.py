"""Tool for searching session conversation history on demand."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from yeoman_gateway.agent.tools.base import Tool

if TYPE_CHECKING:
    from yeoman_gateway.people.resolver import PeopleResolver
    from yeoman_gateway.session.manager import SessionManager


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
            "Person names can be partial; if multiple people match, use a fuller name."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search term to match against message content.",
                    "minLength": 1,
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
            "required": ["query"],
        }

    async def execute(self, **kwargs: Any) -> str:
        if not self._channel or not self._chat_id:
            return "Error: no chat context set."

        query = str(kwargs.get("query") or "").strip().lower()
        if not query:
            return "Error: query cannot be empty."

        max_messages = int(kwargs.get("max_messages") or 30)
        max_messages = max(1, min(50, max_messages))

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
            now_ms = __import__("time").time_ns() // 1_000_000
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
                now_ms = __import__("time").time_ns() // 1_000_000
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
                            at_ms=now_ms,
                            limit=max_messages,
                            query=query,
                        )
                    except PeopleUnavailable:
                        return "Person lookup is unavailable."
                else:
                    rows = queries.search(chat_ids=chat_ids, query=query, limit=max_messages)
            else:
                rows = queries.search(chat_ids=(self._chat_id,), query=query, limit=max_messages)
            if not rows:
                return f"No matching messages found for '{query}'."
            query_warning = (
                f"No messages matched the query terms '{query}'.\n"
                if person and all(row["query_match_count"] == 0 for row in rows)
                else ""
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
                lines.append(f"[{row['sent_ms']}] [{role}]{attribution}: {history_text(row)}")
            return query_warning + f"Found {len(lines)} matching message(s):\n" + "\n".join(lines)

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
