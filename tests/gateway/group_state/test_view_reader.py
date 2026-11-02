"""Config, hot-path reader and reply wiring for the group-state views."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from loguru import logger
from pydantic import ValidationError
from yeoman_gateway.adapters.responder_llm import LLMResponder
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.group_state.contract import ReaderContext
from yeoman_gateway.group_state.view_reader import GroupStateViewReader
from yeoman_gateway.group_state.views import ViewTarget
from yeoman_gateway.providers.base import LLMProvider, LLMResponse
from yeoman_shared.config.schema import Config, GroupStateConfig

from tests.gateway.group_state.test_views import CHAT, LULL, M1, _full_state, _reader, _target

MINUTE = 60_000


class _Store:
    def __init__(self, state: Any = None, *, error: Exception | None = None) -> None:
        self.state = state
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def get(self, channel: str, chat_id: str) -> Any:
        self.calls.append((channel, chat_id))
        if self.error is not None:
            raise self.error
        return self.state


def _settings(
    *,
    reply_mode: str = "off",
    writer_mode: str = "off",
    reply_chats: tuple[str, ...] = (CHAT,),
    writer_chats: tuple[str, ...] = (CHAT,),
    build_chats: tuple[str, ...] = (CHAT,),
    max_chars: int = 700,
    max_state_age_seconds: int = 900,
) -> GroupStateConfig:
    return GroupStateConfig.model_validate(
        {
            "mode": "shadow",
            "chats": [f"whatsapp:{chat}" for chat in build_chats],
            "views": {
                "reply": {
                    "mode": reply_mode,
                    "chats": [f"whatsapp:{chat}" for chat in reply_chats],
                    "maxChars": max_chars,
                    "maxStateAgeSeconds": max_state_age_seconds,
                },
                "writer": {
                    "mode": writer_mode,
                    "chats": [f"whatsapp:{chat}" for chat in writer_chats],
                    "maxChars": max_chars,
                    "maxStateAgeSeconds": max_state_age_seconds,
                },
            },
        }
    )


def _reader_for(store: _Store, settings: GroupStateConfig) -> GroupStateViewReader:
    return GroupStateViewReader(
        store=store,  # type: ignore[arg-type]
        settings=settings,
        lull_ms=LULL,
        clock_ms=lambda: 10_000_000,
    )


def _capture_logs() -> tuple[list[str], int]:
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(message.record["message"]), level="INFO")
    return lines, sink


def test_config_views_default_off() -> None:
    views = Config().processing.group_state.views

    assert views.reply.mode == "off"
    assert views.reply.chats == []
    assert views.reply.max_chars == 700
    assert views.reply.max_state_age_seconds == 900
    assert views.writer.mode == "off"
    assert views.writer.chats == []
    assert views.writer.max_chars == 700
    assert views.writer.max_state_age_seconds == 900


def test_config_views_forbid_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        GroupStateConfig.model_validate({"views": {"reply": {"enabled": True}}})
    with pytest.raises(ValidationError):
        GroupStateConfig.model_validate({"views": {"writer": {"mode": "maybe"}}})


def test_reader_off_does_not_touch_store() -> None:
    store = _Store(_full_state())
    reader = _reader_for(store, _settings(reply_mode="off"))

    text, rendered = reader.render("reply", reader=_reader(), target=_target(M1))

    assert (text, rendered) == (None, None)
    assert store.calls == []


def test_reader_shadow_logs_returns_no_text() -> None:
    store = _Store(_full_state())
    reader = _reader_for(store, _settings(reply_mode="shadow", max_chars=2_000))
    lines, sink = _capture_logs()
    try:
        text, rendered = reader.render("reply", reader=_reader(), target=_target(M1))
    finally:
        logger.remove(sink)

    assert text is None
    assert rendered is not None
    assert rendered.text.startswith("[Group State]")
    assert store.calls == [("whatsapp", CHAT)]
    view_line = next(line for line in lines if line.startswith("group_state_view view=reply"))
    for part in (
        f"chat={CHAT}",
        "mode=shadow",
        "fallback=none",
        "chars=",
        "sections=",
        "items=",
        "state_read_ms=",
        "render_ms=",
    ):
        assert part in view_line


def test_reader_on_returns_text() -> None:
    store = _Store(_full_state())
    reader = _reader_for(store, _settings(reply_mode="on", max_chars=2_000))

    text, rendered = reader.render("reply", reader=_reader(), target=_target(M1))

    assert rendered is not None
    assert text == rendered.text
    assert text is not None and text.startswith("[Group State]")


def test_reader_store_error_returns_none() -> None:
    store = _Store(error=RuntimeError("synthetic store failure"))
    reader = _reader_for(store, _settings(reply_mode="on"))
    lines, sink = _capture_logs()
    try:
        text, rendered = reader.render("reply", reader=_reader(), target=_target(M1))
    finally:
        logger.remove(sink)

    assert (text, rendered) == (None, None)
    assert any(
        line.startswith("group_state_view view=reply") and "fallback=error" in line
        for line in lines
    )


def test_reader_unlisted_chat_none() -> None:
    store = _Store(_full_state())
    reader = _reader_for(store, _settings(reply_mode="on", reply_chats=("other@g.us",)))

    text, rendered = reader.render("reply", reader=_reader(), target=_target(M1))

    assert (text, rendered) == (None, None)
    assert store.calls == []


def test_reader_unmanaged_chat_falls_back() -> None:
    store = _Store(_full_state())
    reader = _reader_for(store, _settings(reply_mode="on", build_chats=("other@g.us",)))
    lines, sink = _capture_logs()
    try:
        text, rendered = reader.render("reply", reader=_reader(), target=_target(M1))
    finally:
        logger.remove(sink)

    assert (text, rendered) == (None, None)
    assert store.calls == []
    assert any("fallback=unmanaged" in line for line in lines)


def test_bootstrap_builds_the_reader_only_when_a_view_is_not_off() -> None:
    from yeoman_gateway.app.bootstrap import _build_group_state_view_reader

    class _Service:
        store = _Store(_full_state())

    config = Config()
    config.processing.group_state.mode = "shadow"
    config.processing.group_state.chats = [f"whatsapp:{CHAT}"]

    assert _build_group_state_view_reader(config, _Service()) is None

    config.processing.group_state.views.writer.mode = "shadow"

    reader = _build_group_state_view_reader(config, _Service())
    assert isinstance(reader, GroupStateViewReader)


class _Provider(LLMProvider):
    def __init__(self) -> None:
        super().__init__()

    async def chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
        return LLMResponse(content="unused")

    def get_default_model(self) -> str:
        return "test/view"


class _FakeReader:
    def __init__(self, text: str | None, rendered: Any = object()) -> None:
        self.text = text
        self.rendered = rendered
        self.calls: list[tuple[str, ReaderContext, ViewTarget]] = []

    def render(
        self, view: str, *, reader: ReaderContext, target: ViewTarget
    ) -> tuple[str | None, Any]:
        self.calls.append((view, reader, target))
        return self.text, self.rendered


def _responder(tmp_path: Path) -> LLMResponder:
    return LLMResponder(bus=MessageBus(), provider=_Provider(), workspace=tmp_path)


def test_responder_reply_metadata_has_block_only_when_on(tmp_path: Path) -> None:
    responder = _responder(tmp_path)
    metadata: dict[str, Any] = {
        "message_id": "native-current",
        "reply_to_message_id": "native-quoted",
    }

    without = dict(metadata)
    responder._attach_group_state_reply(
        without, channel="whatsapp", chat_id=CHAT, sender_id="c1", is_owner=False
    )
    assert "group_state_block" not in without

    shadow_reader = _FakeReader(None)
    responder.set_group_state_views(shadow_reader)
    shadow = dict(metadata)
    responder._attach_group_state_reply(
        shadow, channel="whatsapp", chat_id=CHAT, sender_id="c1", is_owner=False
    )
    assert "group_state_block" not in shadow
    assert shadow_reader.calls[0][0] == "reply"

    on_reader = _FakeReader("[Group State] synthetic block")
    responder.set_group_state_views(on_reader)
    enabled = dict(metadata)
    responder._attach_group_state_reply(
        enabled, channel="whatsapp", chat_id=CHAT, sender_id="c1", is_owner=False
    )
    assert enabled["group_state_block"] == "[Group State] synthetic block"
    view, context, target = on_reader.calls[0]
    assert view == "reply"
    assert (context.channel, context.chat_id, context.principal) == (
        "whatsapp",
        CHAT,
        "c1",
    )
    assert target == ViewTarget(
        message_id=f"whatsapp:{CHAT}:native-current",
        reply_to_message_id=f"whatsapp:{CHAT}:native-quoted",
    )
