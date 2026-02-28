"""TTS capability executor for routed text-to-speech synthesis."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import uuid
from pathlib import Path
from urllib.parse import quote

import httpx
from loguru import logger

from yeoman.media.router import ResolvedProfile


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


_OPENROUTER_AUDIO_FORMATS = {"wav", "mp3", "flac", "opus", "pcm16"}

# OpenAI only supports pcm16 when stream=true (all other formats require non-streaming,
# but OpenRouter requires streaming for audio output).  We always request pcm16 and
# convert to OGG/Opus ourselves so WhatsApp can play it as a voice note.
_OPENROUTER_STREAM_FORMAT = "pcm16"
_OPENROUTER_PCM_SAMPLE_RATE = 24_000  # OpenAI audio models output at 24 kHz


def _pcm16_to_ogg_opus(pcm_data: bytes) -> bytes | None:
    """Convert raw signed-16-bit LE mono PCM to OGG/Opus via ffmpeg subprocess.

    Returns None if ffmpeg is not available or conversion fails.
    """
    import subprocess
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".pcm", delete=False) as pcm_f:
        pcm_f.write(pcm_data)
        pcm_path = pcm_f.name
    ogg_path = pcm_path + ".ogg"
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-f", "s16le",
                "-ar", str(_OPENROUTER_PCM_SAMPLE_RATE),
                "-ac", "1",
                "-i", pcm_path,
                "-c:a", "libopus",
                "-b:a", "32k",
                ogg_path,
            ],
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            logger.error("ffmpeg PCM→OGG failed: {}", result.stderr.decode(errors="replace"))
            return None
        with open(ogg_path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        logger.error("ffmpeg not found — cannot convert PCM16 to OGG/Opus")
        return None
    except Exception as e:
        logger.error("ffmpeg PCM→OGG error {}: {}", e.__class__.__name__, e)
        return None
    finally:
        for p in (pcm_path, ogg_path):
            try:
                os.unlink(p)
            except OSError:
                pass


class OpenRouterAudioTTSProvider:
    """TTS via OpenRouter chat completions with audio output modality.

    Uses the ``/chat/completions`` endpoint with ``modalities: ["audio"]``
    and extracts base64-encoded audio from the response.  Compatible with
    any OpenRouter model that supports audio output (e.g.
    ``openai/gpt-4o-mini-audio-preview``, ``openai/gpt-audio``).
    """

    _SYSTEM_PROMPT = (
        "You are a text-to-speech engine. "
        "Speak the user's message exactly as provided, word for word, "
        "with no additions, omissions, or commentary."
    )

    def __init__(
        self,
        api_key: str | None = None,
        *,
        api_base: str | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
        base = (api_base or "https://openrouter.ai/api/v1").rstrip("/")
        self.api_url = base + "/chat/completions"
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
            logger.warning("OpenRouter API key not configured for audio TTS")
            return None, "openrouter_audio_api_key_missing"

        # OpenAI only allows pcm16 in streaming mode; we convert to OGG/Opus after.
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            **(self.extra_headers or {}),
        }
        payload: dict[str, object] = {
            "model": model,
            "modalities": ["text", "audio"],
            "audio": {"voice": voice, "format": _OPENROUTER_STREAM_FORMAT},
            "stream": True,
            "messages": [
                {"role": "system", "content": self._SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
        }

        audio_chunks: list[str] = []
        try:
            async with httpx.AsyncClient() as client:
                async with client.stream(
                    "POST",
                    self.api_url,
                    headers=headers,
                    json=payload,
                    timeout=self.timeout_seconds,
                ) as response:
                    if response.status_code >= 400:
                        raw = await response.aread()
                        raw_text = raw.decode(errors="replace")
                        # Unwrap nested provider error if present.
                        detail = raw_text
                        try:
                            parsed = json.loads(raw_text)
                            inner = parsed.get("error", {})
                            metadata = inner.get("metadata", {})
                            detail = (
                                metadata.get("raw")
                                or inner.get("message")
                                or raw_text
                            )
                        except Exception:
                            pass
                        logger.error(
                            "OpenRouter audio TTS HTTP {}: {}",
                            response.status_code,
                            detail,
                        )
                        return None, f"openrouter_audio_http_{response.status_code}"

                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data = line[6:]
                        if data.strip() == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                            delta = chunk["choices"][0]["delta"]
                            audio = delta.get("audio")
                            if audio and "data" in audio:
                                audio_chunks.append(audio["data"])
                        except Exception:
                            continue
        except Exception as e:
            logger.error("OpenRouter audio TTS request failed {}: {}", e.__class__.__name__, e)
            return None, f"openrouter_audio_request_failed:{e.__class__.__name__}"

        if not audio_chunks:
            logger.error("OpenRouter audio TTS: no audio data received in stream")
            return None, "openrouter_audio_no_data"

        try:
            pcm_bytes = base64.b64decode("".join(audio_chunks))
        except Exception as e:
            logger.error("OpenRouter audio TTS failed to decode PCM16: {}", e)
            return None, f"openrouter_audio_decode_failed:{e.__class__.__name__}"

        ogg_bytes = _pcm16_to_ogg_opus(pcm_bytes)
        if ogg_bytes is None:
            return None, "openrouter_audio_pcm_convert_failed"
        return ogg_bytes, None


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
        openrouter_api_key: str | None = None,
        openrouter_api_base: str | None = None,
        openrouter_extra_headers: dict[str, str] | None = None,
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
        self._openrouter_api_key = openrouter_api_key
        self._openrouter_api_base = openrouter_api_base
        self._openrouter_extra_headers = openrouter_extra_headers
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

        if provider in {"openrouter_audio"}:
            model = profile.model or "openai/gpt-4o-mini-audio-preview"
            client = OpenRouterAudioTTSProvider(
                api_key=self._openrouter_api_key,
                api_base=self._openrouter_api_base,
                extra_headers=self._openrouter_extra_headers,
                timeout_seconds=timeout_s,
            )
            audio, error = await client.synthesize(text=text, model=model, voice=voice, format=format)
            return (audio or None), error

        return None, f"tts_provider_unsupported:{provider}"
