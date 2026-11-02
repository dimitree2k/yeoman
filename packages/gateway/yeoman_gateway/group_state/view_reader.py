"""Hot-path reader for the reply and writer group-state views.

The read is deliberately tiny: one ``GroupStateStore.get`` (a single SQLite row and a
JSON decode) plus a pure render.  The only archive read is the history-proven
membership snapshot needed for a group Knowledge read; there is no message scan, no
model call and no await on a lease, and it never raises into the reply path: every
failure becomes no block.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Literal

from loguru import logger
from yeoman_shared.config.schema import GroupStateConfig, GroupStateViewConfig

from .contract import ReaderContext
from .models import GroupState
from .render import RenderedState
from .store import GroupStateStore
from .views import (
    ViewTarget,
    conversation_speakers,
    render_reply_view,
    render_writer_view,
    select_turn_people,
)


def _default_clock_ms() -> int:
    return int(time.time() * 1000)


def _principal_of(value: object) -> str | None:
    """Normalise a stored sender value to the roster principal it can name.

    Never invents an identity: only a well-formed whatsapp principal, phone jid, LID
    or bare number becomes one.  Provenance is still checked by ``person_for_principal``.
    """
    text = str(value or "").strip()
    if not text:
        return None
    if text.startswith("whatsapp:"):
        return text
    if text.endswith("@s.whatsapp.net") or text.endswith("@c.us"):
        digits = text.split("@", 1)[0]
        return f"whatsapp:{digits}" if digits else None
    if text.endswith("@lid") or text.isdigit():
        return f"whatsapp:{text}"
    return None


class GroupStateViewReader:
    """Render one view for one chat, obeying ``off | shadow | on`` and its allowlist."""

    def __init__(
        self,
        *,
        store: GroupStateStore,
        settings: GroupStateConfig,
        lull_ms: int,
        clock_ms: Callable[[], int] | None = None,
        knowledge: Any | None = None,
        history_queries: Callable[[], Any | None] | None = None,
    ) -> None:
        self._store = store
        self._settings = settings
        self._lull_ms = max(1, int(lull_ms))
        self._clock = clock_ms or _default_clock_ms
        self._knowledge = knowledge
        self._history_queries = history_queries
        self._managed = {
            (channel, chat_id)
            for channel, _, chat_id in (
                entry.strip().partition(":") for entry in settings.chats if entry.strip()
            )
            if channel and chat_id
        }

    def render(
        self,
        view: Literal["reply", "writer"],
        *,
        reader: ReaderContext,
        target: ViewTarget,
    ) -> tuple[str | None, RenderedState | None]:
        """The text to send (``on`` only), plus the rendered state for logging/tests."""
        try:
            config = self._config_for(view)
            if config is None or config.mode == "off":
                return None, None
            if f"{reader.channel}:{reader.chat_id}" not in {
                str(entry).strip() for entry in config.chats if str(entry).strip()
            }:
                return None, None
            if (reader.channel, reader.chat_id) not in self._managed:
                self._log(
                    view,
                    reader,
                    config.mode,
                    fallback="unmanaged",
                    chars=0,
                    sections=0,
                    items=0,
                    state_read_ms=0,
                    render_ms=0,
                )
                return None, None
        except Exception:  # noqa: BLE001 - the hot path must never raise
            return None, None

        read_started = time.perf_counter()
        try:
            state = self._store.get(reader.channel, reader.chat_id)
        except Exception:  # noqa: BLE001 - a broken store means no block
            self._log(
                view,
                reader,
                config.mode,
                fallback="error",
                chars=0,
                sections=0,
                items=0,
                state_read_ms=int((time.perf_counter() - read_started) * 1000),
                render_ms=0,
            )
            return None, None
        state_read_ms = int((time.perf_counter() - read_started) * 1000)

        render_started = time.perf_counter()
        try:
            now_ms = int(self._clock())
            people, people_names = self._people_notes(config, reader, target, state, now_ms)
            past = self._past_items(config, state)
            renderer = render_reply_view if view == "reply" else render_writer_view
            rendered = renderer(
                state,
                reader=reader,
                target=target,
                now_ms=now_ms,
                max_chars=int(config.max_chars),
                max_age_ms=int(config.max_state_age_seconds) * 1000,
                lull_ms=self._lull_ms,
                people=people,
                people_names=people_names,
                past=past,
            )
        except Exception:  # noqa: BLE001 - a renderer bug means today's prompt
            self._log(
                view,
                reader,
                config.mode,
                fallback="error",
                chars=0,
                sections=0,
                items=0,
                state_read_ms=state_read_ms,
                render_ms=int((time.perf_counter() - render_started) * 1000),
            )
            return None, None
        render_ms = int((time.perf_counter() - render_started) * 1000)

        self._log(
            view,
            reader,
            config.mode,
            fallback=rendered.fallback or "none",
            chars=len(rendered.text),
            sections=len(rendered.sections),
            items=len(rendered.items),
            state_read_ms=state_read_ms,
            render_ms=render_ms,
        )
        if config.mode == "shadow":
            return None, rendered
        return (rendered.text or None), rendered

    def _people_notes(
        self,
        config: GroupStateViewConfig,
        reader: ReaderContext,
        target: ViewTarget,
        state: GroupState,
        now_ms: int,
    ) -> tuple[tuple[Any, ...], dict[str, str]]:
        """Active notes for the turn people, through one history-proven membership read.

        The only archive read on this path is ``members`` (and, when a visible
        identifier needs mapping, ``member_principal``).  There is no message scan,
        no model call and no write.
        """
        if getattr(config, "people", "off") != "on":
            return (), {}
        if self._knowledge is None or self._history_queries is None:
            return (), {}
        queries = self._history_queries()
        if queries is None:
            return (), {}
        audience = queries.members(chat_id=reader.chat_id, at_ms=int(now_ms))
        if audience.status != "known" or not audience.members:
            return (), {}
        values = select_turn_people(target, conversation_speakers(state))
        if not values:
            return (), {}
        from yeoman_gateway.knowledge.read_context import build_group_read_context

        context = build_group_read_context(
            queries=queries,
            channel=reader.channel,
            chat_id=reader.chat_id,
            principal=reader.principal,
            is_owner=bool(reader.is_owner),
            at_ms=int(now_ms),
            policy_revision=int(getattr(self._knowledge, "policy_revision", 0) or 0),
            purpose="reply",
        )
        if context is None:
            return (), {}
        if context.principal_id not in audience.members:
            # Arvid is not a roster member; the writer view still reads only what a
            # current member of this chat may read, through that member's own proof.
            context = build_group_read_context(
                queries=queries,
                channel=reader.channel,
                chat_id=reader.chat_id,
                principal=sorted(audience.members)[0],
                is_owner=False,
                at_ms=int(now_ms),
                policy_revision=int(getattr(self._knowledge, "policy_revision", 0) or 0),
                purpose="reply",
            )
            if context is None:
                return (), {}
        person_ids: list[str] = []
        for value in values:
            person_id = self._person_id(value)
            if person_id and person_id not in person_ids:
                person_ids.append(person_id)
        if not person_ids:
            return (), {}
        notes = self._knowledge.person_notes(
            channel=reader.channel,
            chat_id=reader.chat_id,
            person_ids=tuple(person_ids),
            context=context,
        )
        if not notes:
            return (), {}
        order = {person_id: index for index, person_id in enumerate(person_ids)}
        notes = tuple(sorted(notes, key=lambda note: order.get(str(note.person_id), len(order))))
        names: dict[str, str] = {}
        for note in notes:
            name = self._knowledge.display_name(note.person_id, context=context, for_group=True)
            if name:
                names[str(note.person_id)] = str(name)
        return tuple(notes), names

    def _person_id(self, value: str) -> str | None:
        """The canonical person behind a roster principal, jid or contact id."""
        principal = _principal_of(value)
        if principal:
            person_id = self._knowledge.person_for_principal(principal)
            if person_id:
                return str(person_id)
        try:
            return str(self._knowledge.canonical_id(str(value)))
        except Exception:  # noqa: BLE001 - an unresolved value is simply not a person
            return None

    def _past_items(self, config: GroupStateViewConfig, state: GroupState) -> tuple[Any, ...]:
        if getattr(config, "past", "off") != "on":
            return ()
        section = state.sections.get("past")
        return tuple(section.items) if section is not None else ()

    def _config_for(self, view: str) -> GroupStateViewConfig | None:
        if view not in ("reply", "writer"):
            return None
        views = getattr(self._settings, "views", None)
        return getattr(views, view, None) if views is not None else None

    @staticmethod
    def _log(
        view: str,
        reader: ReaderContext,
        mode: str,
        *,
        fallback: str,
        chars: int,
        sections: int,
        items: int,
        state_read_ms: int,
        render_ms: int,
    ) -> None:
        try:
            logger.info(
                "group_state_view view={} chat={} mode={} fallback={} chars={} sections={} "
                "items={} state_read_ms={} render_ms={}",
                view,
                reader.chat_id,
                mode,
                fallback,
                chars,
                sections,
                items,
                state_read_ms,
                render_ms,
            )
        except Exception:  # noqa: BLE001 - logging must not break the reply path
            pass


__all__ = ["GroupStateViewReader"]
