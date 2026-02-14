"""TTS capability executor for routed text-to-speech synthesis."""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from pathlib import Path
from urllib.parse import quote

import httpx
from loguru import logger

from nanobot.media.router import ResolvedProfile


def strip_markdown_for_tts(text: str) -> str:
    """Best-effort markdown -> plain text for speech synthesis."""
    if not text:
        return ""

    # Drop fenced code blocks.
    text = re.sub(r"```[\s\S]*?```", " ", text)
    # Unwrap inline code.
    text = re.sub(r"`([^`]+)`", r"\1", text)
    # Convert markdown links to visible text.
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", text)

    # Collapse whitespace.
    return " ".join(text.split()).strip()


def truncate_for_voice(text: str, *, max_sentences: int, max_chars: int) -> str:
    """Deterministically truncate assistant text to fit voice guardrails."""
    cleaned = " ".join((text or "").split()).strip()
    if not cleaned:
        return ""

    max_sentences = max(1, int(max_sentences))
    max_chars = max(1, int(max_chars))

    normalized = cleaned.replace("!", ".").replace("?", ".")
    sentences = [s.strip() for s in normalized.split(".") if s.strip()]
    if sentences:
        candidate = ". ".join(sentences[:max_sentences]).strip()
        if candidate and candidate[-1] not in ".!?":
            candidate += "."
    else:
        candidate = cleaned

    if len(candidate) <= max_chars:
        return candidate

    ellipsis = "..."
    if max_chars <= len(ellipsis):
        return candidate[:max_chars].rstrip()

    clipped = candidate[: max_chars - len(ellipsis)].rstrip()
    if " " in clipped:
        clipped = clipped.rsplit(" ", 1)[0].rstrip()
    if not clipped:
        clipped = candidate[: max_chars - len(ellipsis)].rstrip()
    clipped = clipped.rstrip(" .")
    if not clipped:
        return candidate[:max_chars].rstrip()
    return clipped + ellipsis


def write_tts_audio_file(outgoing_dir: Path, audio_bytes: bytes, *, ext: str = ".ogg") -> Path:
    """Write synthesized audio bytes to a unique file under outgoing_dir."""
    outgoing_dir.mkdir(parents=True, exist_ok=True)
    name = f"tts-{uuid.uuid4().hex}{ext}"
    path = outgoing_dir / name
    path.write_bytes(audio_bytes)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


class OpenAITTSProvider:
    """Text-to-speech provider using OpenAI's audio/speech endpoint."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        api_base: str | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        base = api_base or os.environ.get("OPENAI_API_BASE") or "https://api.openai.com/v1"
        self.api_url = base.rstrip("/") + "/audio/speech"
        self.timeout_seconds = timeout_seconds
        self.extra_headers = extra_headers

    async def synthesize(
        self,
        *,
        text: str,
        model: str,
        voice: str,
        format: str,
    ) -> tuple[bytes | None, str | None]:
        if not self.api_key:
            logger.warning("OpenAI API key not configured for TTS")
            return None, "openai_api_key_missing"

        models = [model]
        if "openrouter.ai" in self.api_url and "/" not in model:
            models.append(f"openai/{model}")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            **(self.extra_headers or {}),
        }

        def payloads_for(model_value: str) -> list[dict[str, object]]:
            base_payload: dict[str, object] = {
                "model": model_value,
                "voice": voice,
                "input": text,
            }
            return [
                {**base_payload, "response_format": format},
                {**base_payload, "format": format},
            ]

        response: httpx.Response | None = None
        async with httpx.AsyncClient() as client:
            for model_value in models:
                for payload in payloads_for(model_value):
                    try:
                        response = await client.post(
                            self.api_url,
                            headers=headers,
                            json=payload,
                            timeout=self.timeout_seconds,
                        )
                    except Exception as e:
                        logger.error("OpenAI TTS request failed {}: {}", e.__class__.__name__, e)
                        return None, f"openai_request_failed:{e.__class__.__name__}"
                    if response.status_code < 400:
                        break
                if response is not None and response.status_code < 400:
                    break

        try:
            if response is None:
                return None, "openai_no_response"
            response.raise_for_status()
        except Exception as e:
            logger.error("OpenAI TTS error {}: {}", e.__class__.__name__, getattr(response, "text", ""))
            status = getattr(response, "status_code", "unknown")
            return None, f"openai_http_{status}"

        return bytes(response.content or b""), None


def _resolve_elevenlabs_output_format(fmt: str) -> str:
    normalized = str(fmt or "").strip().lower()
    if not normalized or normalized == "opus":
        return "opus_48000_64"
    if normalized.startswith("opus_"):
        return normalized
    return "opus_48000_64"


class ElevenLabsTTSProvider:
    """Text-to-speech provider using ElevenLabs text-to-speech API."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        api_base: str | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.api_key = api_key or os.environ.get("ELEVENLABS_API_KEY")
        self.api_base = (
            api_base
            or os.environ.get("ELEVENLABS_API_BASE")
            or "https://api.elevenlabs.io/v1"
        )
        self.timeout_seconds = timeout_seconds
        self.extra_headers = extra_headers

    async def synthesize(
        self,
        *,
        text: str,
        model: str,
        voice: str,
        format: str,
    ) -> tuple[bytes | None, str | None]:
        if not self.api_key:
            logger.warning("ElevenLabs API key not configured for TTS")
            return None, "elevenlabs_api_key_missing"

        voice_id = str(voice or "").strip()
        if not voice_id:
            logger.warning("ElevenLabs voice id is required for TTS")
            return None, "elevenlabs_voice_id_missing"

        url = self.api_base.rstrip("/") + f"/text-to-speech/{quote(voice_id, safe='')}"
        headers = {
            "xi-api-key": self.api_key,
            "Content-Type": "application/json",
            **(self.extra_headers or {}),
        }
        payload: dict[str, object] = {
            "text": text,
            "model_id": model,
        }
        params = {"output_format": _resolve_elevenlabs_output_format(format)}
        response: httpx.Response | None = None

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    url,
                    headers=headers,
                    json=payload,
                    params=params,
                    timeout=self.timeout_seconds,
                )
            except Exception as e:
                logger.error("ElevenLabs TTS request failed {}: {}", e.__class__.__name__, e)
                return None, f"elevenlabs_request_failed:{e.__class__.__name__}"

        try:
            if response is None:
                return None, "elevenlabs_no_response"
            response.raise_for_status()
        except Exception as e:
            logger.error(
                "ElevenLabs TTS error {}: {}",
                e.__class__.__name__,
                getattr(response, "text", ""),
            )
            status = getattr(response, "status_code", "unknown")
            body = str(getattr(response, "text", "") or "").strip().lower()
            if "quota_exceeded" in body:
                return None, "elevenlabs_quota_exceeded"
            return None, f"elevenlabs_http_{status}"

        return bytes(response.content or b""), None


class TTSSynthesizer:
    """Synthesize speech using the route-selected TTS backend."""

    def __init__(
        self,
        *,
        openai_api_key: str | None = None,
        openai_api_base: str | None = None,
        openai_extra_headers: dict[str, str] | None = None,
        elevenlabs_api_key: str | None = None,
        elevenlabs_api_base: str | None = None,
        elevenlabs_extra_headers: dict[str, str] | None = None,
        elevenlabs_default_voice_id: str | None = None,
        elevenlabs_default_model_id: str | None = None,
        max_concurrency: int = 2,
    ) -> None:
        self._openai_api_key = openai_api_key
        self._openai_api_base = openai_api_base
        self._openai_extra_headers = openai_extra_headers
        self._elevenlabs_api_key = elevenlabs_api_key
        self._elevenlabs_api_base = elevenlabs_api_base
        self._elevenlabs_extra_headers = elevenlabs_extra_headers
        self._elevenlabs_default_voice_id = elevenlabs_default_voice_id
        self._elevenlabs_default_model_id = elevenlabs_default_model_id
        self._semaphore = asyncio.Semaphore(max(1, int(max_concurrency)))

    async def synthesize(
        self,
        text: str,
        *,
        profile: ResolvedProfile,
        voice: str,
        format: str,
    ) -> bytes | None:
        audio, _ = await self.synthesize_with_status(
            text,
            profile=profile,
            voice=voice,
            format=format,
        )
        return audio

    async def synthesize_with_status(
        self,
        text: str,
        *,
        profile: ResolvedProfile,
        voice: str,
        format: str,
    ) -> tuple[bytes | None, str | None]:
        async with self._semaphore:
            return await self._synthesize_once(
                text,
                profile=profile,
                voice=voice,
                format=format,
            )

    async def _synthesize_once(
        self,
        text: str,
        *,
        profile: ResolvedProfile,
        voice: str,
        format: str,
    ) -> tuple[bytes | None, str | None]:
        if profile.kind != "tts":
            return None, "tts_profile_kind_mismatch"

        provider = (profile.provider or "openai_tts").strip().lower()
        timeout_s = max(1.0, (profile.timeout_ms or 30000) / 1000.0)

        if provider in {"", "openai_tts"}:
            model = profile.model or "tts-1"
            client = OpenAITTSProvider(
                api_key=self._openai_api_key,
                api_base=self._openai_api_base,
                extra_headers=self._openai_extra_headers,
                timeout_seconds=timeout_s,
            )
            audio, error = await client.synthesize(text=text, model=model, voice=voice, format=format)
            return (audio or None), error
        if provider in {"elevenlabs_tts", "elevenlabs"}:
            model_candidate = str(profile.model or "").strip()
            if not model_candidate or model_candidate.startswith("tts-"):
                model = self._elevenlabs_default_model_id or "eleven_multilingual_v2"
            else:
                model = model_candidate
            voice_candidate = str(voice or "").strip()
            if not voice_candidate or voice_candidate == "alloy":
                voice_candidate = str(self._elevenlabs_default_voice_id or "").strip()
            client = ElevenLabsTTSProvider(
                api_key=self._elevenlabs_api_key,
                api_base=self._elevenlabs_api_base,
                extra_headers=self._elevenlabs_extra_headers,
                timeout_seconds=timeout_s,
            )
            audio, error = await client.synthesize(
                text=text,
                model=model,
                voice=voice_candidate,
                format=format,
            )
            return (audio or None), error

        return None, f"tts_provider_unsupported:{provider}"
