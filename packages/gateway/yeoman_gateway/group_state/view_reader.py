"""Hot-path reader for the reply and writer group-state views.

The read is deliberately tiny: one ``GroupStateStore.get`` (a single SQLite row and a
JSON decode) plus a pure render. It never reads history, calls a model or waits on a
lease, and it never raises into the reply path: every failure becomes no block.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Literal

from loguru import logger
from yeoman_shared.config.schema import GroupStateConfig, GroupStateViewConfig

from .contract import ReaderContext
from .render import RenderedState
from .store import GroupStateStore
from .views import ViewTarget, render_reply_view, render_writer_view


def _default_clock_ms() -> int:
    return int(time.time() * 1000)


class GroupStateViewReader:
    """Render one view for one chat, obeying ``off | shadow | on`` and its allowlist."""

    def __init__(
        self,
        *,
        store: GroupStateStore,
        settings: GroupStateConfig,
        lull_ms: int,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self._store = store
        self._settings = settings
        self._lull_ms = max(1, int(lull_ms))
        self._clock = clock_ms or _default_clock_ms
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
            renderer = render_reply_view if view == "reply" else render_writer_view
            rendered = renderer(
                state,
                reader=reader,
                target=target,
                now_ms=now_ms,
                max_chars=int(config.max_chars),
                max_age_ms=int(config.max_state_age_seconds) * 1000,
                lull_ms=self._lull_ms,
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
