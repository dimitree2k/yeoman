"""make_meme: rendering, templates, generated backgrounds, daily cap, tool and policy gate."""

from __future__ import annotations

import base64
import json
import time
from datetime import date
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from PIL import Image
from yeoman_gateway.agent.tools.make_meme import MakeMemeTool
from yeoman_gateway.bus.events import OutboundMessage
from yeoman_gateway.media.document_cache import DocumentCache
from yeoman_gateway.media.memes.background import BackgroundError, BackgroundGenerator
from yeoman_gateway.media.memes.render import TextBox, render_meme
from yeoman_gateway.media.memes.service import DailyCap, MemeError, MemeService
from yeoman_gateway.media.memes.templates import load_templates
from yeoman_gateway.media.router import ModelRouter, ResolvedProfile
from yeoman_gateway.policy.engine import ActorContext, PolicyEngine
from yeoman_gateway.policy.schema import PolicyConfig
from yeoman_gateway.processing.tool_context import CURRENT_TOOL_CONTEXT, ToolInvocationContext
from yeoman_gateway.providers.factory import ProviderCredentials
from yeoman_shared.config.schema import ModelRoutingConfig

GROUP = "123@g.us"


def _white(width: int = 400, height: int = 300) -> Image.Image:
    return Image.new("RGB", (width, height), "white")


def _ink_rows(image: Image.Image) -> list[int]:
    """Rows that contain dark (outline) pixels."""
    gray = image.convert("L")
    width, height = gray.size
    return [y for y in range(height) if min(gray.getpixel((x, y)) for x in range(width)) < 60]


# --- rendering ---------------------------------------------------------------------


def test_render_draws_caption_only_inside_its_box() -> None:
    out = render_meme(_white(), ["hello"], (TextBox(anchor_y=0.0, scale_y=0.25),))
    rows = _ink_rows(out)
    assert rows, "caption was not drawn"
    assert max(rows) < 0.25 * out.height


@pytest.mark.parametrize("caption", ["Пятничный деплой", "Grüße aus Straße", "gjpqy ЙДЦЩу"])
def test_render_keeps_accents_and_descenders_inside_the_box(caption: str) -> None:
    box = TextBox(anchor_y=0.4, scale_y=0.2)
    out = render_meme(_white(), [caption], (box,))
    rows = _ink_rows(out)
    assert rows
    assert min(rows) >= 0.4 * out.height - 1
    assert max(rows) <= 0.6 * out.height + 1


def test_render_shrinks_long_text_instead_of_dropping_it() -> None:
    long = "this caption is far too long " * 6
    out = render_meme(_white(), [long, "bottom"])
    assert _ink_rows(out)


def test_render_scales_down_large_images() -> None:
    out = render_meme(_white(3000, 2000), ["x"])
    assert max(out.size) == 1024


def test_black_text_gets_a_light_outline() -> None:
    box = TextBox(anchor_y=0.0, scale_y=0.5, color="black")
    out = render_meme(Image.new("RGB", (300, 200), "black"), ["HELLO"], (box,))
    # On a black image, only a white outline makes the caption visible at all.
    assert out.convert("L").getextrema()[1] > 200


# --- templates ---------------------------------------------------------------------


def test_vendored_templates_load_with_boxes() -> None:
    templates = load_templates()
    assert len(templates) >= 30
    assert len(templates["drake"].boxes) == 2
    assert len(templates["db"].boxes) == 3
    for template in templates.values():
        assert template.image_path.is_file()


# --- generated backgrounds ---------------------------------------------------------


def _png_data_url() -> str:
    buffer = BytesIO()
    Image.new("RGB", (64, 64), "blue").save(buffer, "PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


def _profile(kind: str = "image") -> ResolvedProfile:
    return ResolvedProfile(
        route_key="meme.background",
        profile_name="meme_background",
        kind=kind,
        model="black-forest-labs/flux.2-klein-4b",
        provider="openrouter",
        max_tokens=None,
        temperature=None,
        timeout_ms=10_000,
    )


def _generator(handler) -> BackgroundGenerator:
    return BackgroundGenerator(
        ProviderCredentials(
            api_key="test-key",
            api_base="https://openrouter.test/api/v1",
            extra_headers=None,
            provider_name="openrouter",
        ),
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
async def test_background_requests_image_only_and_forbids_text() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        message = {"role": "assistant", "images": [{"image_url": {"url": _png_data_url()}}]}
        return httpx.Response(200, json={"choices": [{"message": message}]})

    image = await _generator(handler).generate("a cat in space", _profile())

    assert image.size == (64, 64)
    body = seen["body"]
    assert seen["url"] == "https://openrouter.test/api/v1/chat/completions"
    assert body["model"] == "black-forest-labs/flux.2-klein-4b"
    assert body["modalities"] == ["image"]
    prompt = body["messages"][0]["content"]
    assert prompt.startswith("a cat in space")
    assert "no text" in prompt


@pytest.mark.asyncio
async def test_background_reports_http_errors_and_missing_images() -> None:
    with pytest.raises(BackgroundError, match="HTTP 402"):
        await _generator(lambda r: httpx.Response(402, json={})).generate("x", _profile())
    with pytest.raises(BackgroundError, match="no image"):
        await _generator(
            lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})
        ).generate("x", _profile())
    with pytest.raises(BackgroundError, match="not an image profile"):
        await _generator(lambda r: httpx.Response(500)).generate("x", _profile("chat"))


@pytest.mark.asyncio
async def test_daily_cap_counts_per_day_and_refunds(tmp_path: Path) -> None:
    today = date(2026, 10, 28)
    cap = DailyCap(tmp_path / "quota.json", 2, today=lambda: today)
    assert await cap.claim()
    assert await cap.claim()
    assert not await cap.claim()
    await cap.refund()
    assert await cap.claim()
    today = date(2026, 10, 29)
    assert await cap.claim()
    assert json.loads((tmp_path / "quota.json").read_text()) == {"2026-10-29": 1}


@pytest.mark.asyncio
async def test_failed_background_does_not_use_up_the_cap(tmp_path: Path) -> None:
    routing = ModelRoutingConfig.model_validate({})
    cap = DailyCap(tmp_path / "quota.json", 1)
    service = MemeService(
        outgoing_dir=tmp_path / "out",
        router=ModelRouter(routing),
        background=_generator(lambda r: httpx.Response(503)),
        background_cap=cap,
    )
    with pytest.raises(MemeError, match="background generation failed"):
        await service.from_background("x", ["top", "bottom"], "whatsapp")
    assert await cap.claim(), "a failed generation must give its slot back"


# --- tool --------------------------------------------------------------------------


class _Sent:
    def __init__(self) -> None:
        self.messages: list[OutboundMessage] = []

    async def __call__(self, message: OutboundMessage) -> None:
        self.messages.append(message)


def _photo(tmp_path: Path, cache: DocumentCache, chat_id: str, message_id: str, *, age: int = 0):
    path = tmp_path / f"{message_id}.jpg"
    Image.new("RGB", (320, 240), "green").save(path)
    cache.record_media_item(
        channel="whatsapp",
        chat_id=chat_id,
        message_id=message_id,
        sender_id="s",
        sender_name="Sam",
        kind="image",
        mime_type="image/jpeg",
        file_name=None,
        local_path=path,
        size_bytes=path.stat().st_size,
        timestamp=int(time.time()) - age,
    )
    return path


@pytest.fixture
def tool_setup(tmp_path: Path):
    cache = DocumentCache(tmp_path / "cache.db")
    sent = _Sent()
    service = MemeService(outgoing_dir=tmp_path / "outgoing")
    tool = MakeMemeTool(service=service, send_callback=sent, media_cache=cache)
    return tool, cache, sent, tmp_path


def _in_chat(chat_id: str = GROUP, reply_to: str | None = None, channel: str = "whatsapp"):
    return CURRENT_TOOL_CONTEXT.set(
        ToolInvocationContext(channel=channel, chat_id=chat_id, reply_to_message_id=reply_to)
    )


@pytest.mark.asyncio
async def test_template_meme_is_sent_as_photo_to_current_chat(tool_setup) -> None:
    tool, _cache, sent, tmp_path = tool_setup
    token = _in_chat()
    try:
        result = await tool.execute(mode="template", template="drake", captions=["a", "b"])
    finally:
        CURRENT_TOOL_CONTEXT.reset(token)
    assert result.startswith("Meme delivered")
    (message,) = sent.messages
    assert message.chat_id == GROUP and message.content == ""
    path = Path(message.media[0])
    assert path.parent == tmp_path / "outgoing" / "memes" and path.suffix == ".jpg"


@pytest.mark.asyncio
async def test_photo_meme_uses_replied_photo_and_replies_to_it(tool_setup) -> None:
    tool, cache, sent, tmp_path = tool_setup
    _photo(tmp_path, cache, GROUP, "photo-1", age=5 * 86400)
    token = _in_chat(reply_to="photo-1")
    try:
        result = await tool.execute(mode="photo", captions=["top", "bottom"])
    finally:
        CURRENT_TOOL_CONTEXT.reset(token)
    assert result.startswith("Meme delivered")
    assert sent.messages[0].reply_to == "photo-1"


@pytest.mark.asyncio
async def test_photo_from_another_chat_is_never_used(tool_setup) -> None:
    tool, cache, sent, tmp_path = tool_setup
    _photo(tmp_path, cache, "other@g.us", "photo-x")
    token = _in_chat(reply_to="photo-x")
    try:
        result = await tool.execute(mode="photo", captions=["top"])
    finally:
        CURRENT_TOOL_CONTEXT.reset(token)
    assert "no photo I can still use" in result
    assert not sent.messages


@pytest.mark.asyncio
async def test_deleted_photo_file_gives_a_friendly_error(tool_setup) -> None:
    tool, cache, sent, tmp_path = tool_setup
    _photo(tmp_path, cache, GROUP, "photo-2").unlink()
    token = _in_chat(reply_to="photo-2")
    try:
        result = await tool.execute(mode="photo", captions=["top"])
    finally:
        CURRENT_TOOL_CONTEXT.reset(token)
    assert "already been deleted" in result
    assert not sent.messages


@pytest.mark.asyncio
async def test_without_reply_only_a_very_recent_photo_is_used(tool_setup) -> None:
    tool, cache, sent, tmp_path = tool_setup
    _photo(tmp_path, cache, GROUP, "old", age=3600)
    token = _in_chat()
    try:
        assert "reply to the photo" in await tool.execute(mode="photo", captions=["t"])
        _photo(tmp_path, cache, GROUP, "fresh", age=30)
        assert (await tool.execute(mode="photo", captions=["t"])).startswith("Meme delivered")
    finally:
        CURRENT_TOOL_CONTEXT.reset(token)
    assert sent.messages[0].reply_to == "fresh"


@pytest.mark.asyncio
async def test_tool_rejects_bad_input_without_sending(tool_setup) -> None:
    tool, _cache, sent, _tmp = tool_setup
    token = _in_chat()
    try:
        assert "unknown template" in await tool.execute(
            mode="template", template="nope", captions=["x"]
        )
        assert "caption is required" in await tool.execute(
            mode="template", template="drake", captions=["", " "]
        )
        assert "unknown meme mode" in await tool.execute(mode="background", captions=["x"])
    finally:
        CURRENT_TOOL_CONTEXT.reset(token)
    token = _in_chat(channel="telegram")
    try:
        assert "only supported in WhatsApp" in await tool.execute(
            mode="template", template="drake", captions=["x"]
        )
    finally:
        CURRENT_TOOL_CONTEXT.reset(token)
    assert not sent.messages


def test_background_mode_is_hidden_without_a_generator(tool_setup) -> None:
    tool = tool_setup[0]
    assert tool.parameters["properties"]["mode"]["enum"] == ["template", "photo"]
    assert "background_prompt" not in tool.parameters["properties"]
    assert "drake(2)" in tool.description


# --- policy gate -------------------------------------------------------------------


def _actor(chat_id: str) -> ActorContext:
    return ActorContext(
        channel="whatsapp",
        chat_id=chat_id,
        sender_primary="owner@s.whatsapp.net",
        sender_aliases=[],
        is_group=chat_id.endswith("@g.us"),
        mentioned_bot=True,
        reply_to_bot=False,
    )


def test_memes_are_off_unless_the_chat_opts_in(tmp_path: Path) -> None:
    policy = PolicyConfig.model_validate(
        {
            "owners": {"whatsapp": ["owner@s.whatsapp.net"]},
            "ownerTools": ["make_meme"],
            "defaults": {"allowedTools": {"mode": "all"}},
            "channels": {"whatsapp": {"chats": {GROUP: {"memes": True}}}},
        }
    )
    engine = PolicyEngine(policy, apply_channels={"whatsapp"}, workspace=tmp_path)
    tools = {"message", "make_meme"}

    enabled = engine.evaluate(_actor(GROUP), tools)
    disabled = engine.evaluate(_actor("456@g.us"), tools)

    assert "make_meme" in enabled.allowed_tools
    # Neither the default allowedTools "all" nor ownerTools switches memes on.
    assert "make_meme" not in disabled.allowed_tools
    assert "message" in disabled.allowed_tools
