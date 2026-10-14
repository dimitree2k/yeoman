"""Drama 3 markup is added just before speech and only for Drama 3 routes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from yeoman_gateway.media.tts import (
    VOICE_STYLE_PROMPT_PATH,
    FishAudioTTSProvider,
    TTSSynthesizer,
    is_drama_profile,
    load_voice_style_prompt,
    strip_voice_directions,
)
from yeoman_gateway.media.voice_script import VoiceScriptWriter, script_rejection

TEXT = "Hallo! Das ist wichtig. Bis dann."
SCRIPT = "[warm and relaxed] Hallo! Das ist <emphasis>wichtig</emphasis>. [pause] Bis dann."


@dataclass(slots=True)
class _Profile:
    provider: str
    model: str
    kind: str = "tts"
    timeout_ms: int = 30_000


DRAMA = _Profile(provider="fish_audio", model="drama-3")
PLAIN = _Profile(provider="fish_audio", model="s2-pro")


def test_strip_voice_directions_keeps_spoken_words_and_links():
    assert strip_voice_directions(SCRIPT) == TEXT
    assert strip_voice_directions("Siehe [Doku](https://x.y).") == "Siehe [Doku](https://x.y)."


def test_is_drama_profile():
    assert is_drama_profile(DRAMA)
    assert not is_drama_profile(PLAIN)
    assert not is_drama_profile(_Profile(provider="openai_tts", model="drama-3"))
    assert not is_drama_profile(None)


def test_load_voice_style_prompt(tmp_path):
    assert load_voice_style_prompt(tmp_path) == ""
    path = tmp_path / VOICE_STYLE_PROMPT_PATH
    path.parent.mkdir(parents=True)
    path.write_text("  Regeln  \n", encoding="utf-8")
    assert load_voice_style_prompt(tmp_path) == "Regeln"


@pytest.mark.parametrize(
    ("script", "reason"),
    [
        (SCRIPT, None),
        ("[calm] Hallo, das ist wichtig... Bis dann!", None),
        ("", "empty"),
        ("[warm] Hallo! Das ist sehr wichtig. Bis dann.", "words_changed"),
        ("Hallo! Das ist <emphasis>wichtig. Bis dann.", "malformed_tags"),
        ("Hallo! Das ist <loud>wichtig</loud>. Bis dann.", "malformed_tags"),
        ("<soft>Hallo! <fast>Das</fast> ist wichtig.</soft> Bis dann.", "malformed_tags"),
    ],
)
def test_script_rejection(script, reason):
    assert script_rejection(TEXT, script) == reason


class _Client:
    def __init__(self, reply: str | Exception) -> None:
        self.reply = reply
        self.messages: list[list[dict[str, str]]] = []

    async def chat(self, messages: list[dict[str, str]], *, max_tokens: int) -> str:
        self.messages.append(messages)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def _workspace(tmp_path):
    path = tmp_path / VOICE_STYLE_PROMPT_PATH
    path.parent.mkdir(parents=True)
    path.write_text("Mache ein Drama-3-Skript.", encoding="utf-8")
    return tmp_path


@pytest.mark.asyncio
async def test_writer_returns_valid_script(tmp_path):
    client = _Client(SCRIPT)
    writer = VoiceScriptWriter(client=client, workspace=_workspace(tmp_path))

    assert await writer(TEXT) == SCRIPT
    assert client.messages[0][0] == {"role": "system", "content": "Mache ein Drama-3-Skript."}
    assert client.messages[0][1] == {"role": "user", "content": TEXT}


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["[warm] Ganz andere Worte.", RuntimeError("down")])
async def test_writer_falls_back_to_plain_text(tmp_path, reply):
    writer = VoiceScriptWriter(client=_Client(reply), workspace=_workspace(tmp_path))
    assert await writer(TEXT) == TEXT


@pytest.mark.asyncio
async def test_writer_times_out_to_plain_text(tmp_path):
    class _Slow(_Client):
        async def chat(self, messages: list[dict[str, str]], *, max_tokens: int) -> str:
            await asyncio.sleep(5)
            return SCRIPT

    writer = VoiceScriptWriter(
        client=_Slow(SCRIPT), workspace=_workspace(tmp_path), timeout_seconds=0.05
    )
    assert await writer(TEXT) == TEXT


@pytest.mark.asyncio
async def test_writer_without_prompt_file_skips_model(tmp_path):
    client = _Client(SCRIPT)
    writer = VoiceScriptWriter(client=client, workspace=tmp_path)

    assert await writer(TEXT) == TEXT
    assert client.messages == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("profile", "expected"), [(DRAMA, SCRIPT), (PLAIN, TEXT)])
async def test_synthesizer_scripts_only_drama_profiles(monkeypatch, profile, expected):
    spoken: list[str] = []

    async def _fake_once(self: Any, text: str, **_: Any) -> tuple[bytes, None]:
        spoken.append(text)
        return b"ogg", None

    async def _writer(text: str) -> str:
        return SCRIPT

    monkeypatch.setattr(TTSSynthesizer, "_synthesize_once", _fake_once)
    tts = TTSSynthesizer()
    tts.script_writer = _writer

    await tts.synthesize_with_status(TEXT, profile=profile, voice="v", format="opus")

    assert spoken == [expected]


@pytest.mark.asyncio
async def test_fish_sends_model_as_header(monkeypatch):
    seen: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        seen["body"] = request.read()
        return httpx.Response(200, content=b"ogg")

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **_: real_client(transport=httpx.MockTransport(_handler))
    )
    audio, error = await FishAudioTTSProvider("key").synthesize(
        text="Hallo", model="drama-3", voice="v", format="opus"
    )
    assert (audio, error) == (b"ogg", None)
    assert seen["headers"]["model"] == "drama-3"
    assert b'"model"' not in seen["body"]
