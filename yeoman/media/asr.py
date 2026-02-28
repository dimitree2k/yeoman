"""ASR capability executor scaffold for routed voice transcription."""

from __future__ import annotations

import asyncio
from pathlib import Path

from yeoman.media.router import ResolvedProfile
from yeoman.providers.transcription import GroqTranscriptionProvider, OpenAITranscriptionProvider


class ASRTranscriber:
    """Transcribe audio files through route-selected ASR backend."""

    def __init__(
        self,
        *,
        groq_api_key: str | None = None,
        openai_api_key: str | None = None,
        openai_api_base: str | None = None,
        openai_extra_headers: dict[str, str] | None = None,
        max_concurrency: int = 2,
    ) -> None:
        self._groq_api_key = groq_api_key
        self._openai_api_key = openai_api_key
        self._openai_api_base = openai_api_base
        self._openai_extra_headers = openai_extra_headers
        self._semaphore = asyncio.Semaphore(max(1, int(max_concurrency)))

    async def transcribe(self, audio_path: Path, profile: ResolvedProfile) -> str | None:
        async with self._semaphore:
            return await self._transcribe_once(audio_path, profile)

    async def _transcribe_once(self, audio_path: Path, profile: ResolvedProfile) -> str | None:
        if profile.kind != "asr":
            return None
        if not audio_path.exists() or not audio_path.is_file():
            return None

        timeout_s = max(1.0, (profile.timeout_ms or 60000) / 1000.0)
        provider = (profile.provider or "groq_whisper").strip()
        if provider in {"", "groq_whisper"}:
            model = profile.model or "whisper-large-v3"
            transcriber = GroqTranscriptionProvider(
                api_key=self._groq_api_key,
                model=model,
                timeout_seconds=timeout_s,
            )
            text = await transcriber.transcribe(audio_path)
        elif provider == "openai_whisper":
            model = profile.model or "whisper-1"
            if model == "whisper-large-v3":
                model = "whisper-1"
            transcriber = OpenAITranscriptionProvider(
                api_key=self._openai_api_key,
                api_base=self._openai_api_base,
                extra_headers=self._openai_extra_headers,
                model=model,
                timeout_seconds=timeout_s,
            )
            text = await transcriber.transcribe(audio_path)
        else:
            return None
        cleaned = " ".join(text.split())
        return cleaned or None
