"""ASR capability executor scaffold for routed voice transcription."""

from __future__ import annotations

from pathlib import Path

from nanobot.media.router import ResolvedProfile
from nanobot.providers.transcription import GroqTranscriptionProvider


class ASRTranscriber:
    """Transcribe audio files through route-selected ASR backend."""

    def __init__(self, *, groq_api_key: str | None = None) -> None:
        self._groq_api_key = groq_api_key

    async def transcribe(self, audio_path: Path, profile: ResolvedProfile) -> str | None:
        if profile.kind != "asr":
            return None
        if not audio_path.exists() or not audio_path.is_file():
            return None
        if profile.provider and profile.provider != "groq_whisper":
            return None

        model = profile.model or "whisper-large-v3"
        timeout_s = max(1.0, (profile.timeout_ms or 60000) / 1000.0)
        transcriber = GroqTranscriptionProvider(
            api_key=self._groq_api_key,
            model=model,
            timeout_seconds=timeout_s,
        )
        text = await transcriber.transcribe(audio_path)
        cleaned = " ".join(text.split())
        return cleaned or None
