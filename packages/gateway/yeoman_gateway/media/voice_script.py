"""Turn plain voice-note text into a Fish Audio Drama 3 script just before speech.

The assistant writes voice notes as plain text. Only here, right before
synthesis on a Drama 3 route, a small model adds performance markup
([directions], [pause], paired word tags) following the owner-editable prompt
in ``workspace/prompts/voice-drama3.md``. The markup never enters the chat
prompt, the text fallback or the session history.

The rewrite may only add markup. When the script's spoken words differ from
the input, its tags are malformed, or the call fails, the plain text is spoken.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any, Protocol

from loguru import logger

from yeoman_gateway.media.tts import load_voice_style_prompt, strip_voice_directions

VOICE_SCRIPT_ROUTE = "voice.script"

_WORD_RE = re.compile(r"\w+", re.UNICODE)
_PAIRED_TAG_RE = re.compile(r"<(/?)(whisper|emphasis|soft|fast|slow|stress|forceful)>", re.I)
_ANY_TAG_RE = re.compile(r"</?[A-Za-z][^<>]*>")


class _ChatClient(Protocol):
    async def chat(self, messages: list[dict[str, str]], *, max_tokens: int) -> str: ...


def _spoken_words(text: str) -> list[str]:
    return [word.lower() for word in _WORD_RE.findall(strip_voice_directions(text))]


def _tags_well_formed(script: str) -> bool:
    """Only allowed paired tags, each closed, none nested."""
    allowed = {match.group(0).lower() for match in _PAIRED_TAG_RE.finditer(script)}
    if any(tag.group(0).lower() not in allowed for tag in _ANY_TAG_RE.finditer(script)):
        return False
    open_tag: str | None = None
    for match in _PAIRED_TAG_RE.finditer(script):
        closing, name = match.group(1) == "/", match.group(2).lower()
        if not closing:
            if open_tag is not None:
                return False
            open_tag = name
        elif open_tag != name:
            return False
        else:
            open_tag = None
    return open_tag is None


def script_rejection(text: str, script: str) -> str | None:
    """Why a generated script must not be spoken, or None when it is safe."""
    if not script.strip():
        return "empty"
    if not _tags_well_formed(script):
        return "malformed_tags"
    if _spoken_words(script) != _spoken_words(text):
        return "words_changed"
    return None


class VoiceScriptWriter:
    """Adds Drama 3 markup to finished voice text with a small model."""

    def __init__(
        self,
        *,
        client: _ChatClient,
        workspace: Path | None,
        timeout_seconds: float = 8.0,
        max_tokens: int = 800,
    ) -> None:
        self._client = client
        self._workspace = workspace
        self._timeout_seconds = timeout_seconds
        self._max_tokens = max_tokens

    async def __call__(self, text: str) -> str:
        prompt = load_voice_style_prompt(self._workspace)
        if not prompt or not text.strip():
            return text
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": text},
        ]
        try:
            script = await asyncio.wait_for(
                self._client.chat(messages, max_tokens=self._max_tokens),
                timeout=self._timeout_seconds,
            )
        except Exception as exc:
            logger.warning("voice_script fallback reason=error:{}", type(exc).__name__)
            return text
        script = str(script or "").strip()
        rejection = script_rejection(text, script)
        if rejection is not None:
            logger.warning("voice_script fallback reason={}", rejection)
            return text
        return script


def build_voice_script_writer(config: Any, workspace: Path | None) -> VoiceScriptWriter | None:
    """Writer for the ``voice.script`` route, or None when the route is not configured."""
    from yeoman_gateway.processing import model_route

    routes = getattr(getattr(config, "models", None), "routes", None) or {}
    if VOICE_SCRIPT_ROUTE not in routes:
        return None
    try:
        client = model_route.RouteClient(config=config, route_key=VOICE_SCRIPT_ROUTE)
    except model_route.RouteUnavailableError as exc:
        logger.error("voice_script disabled: {}", str(exc)[:160])
        return None
    logger.info("voice_script enabled route={} model={}", VOICE_SCRIPT_ROUTE, client.model)
    return VoiceScriptWriter(client=client, workspace=workspace)
