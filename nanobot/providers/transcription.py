"""Voice transcription providers."""

import os
from pathlib import Path

import httpx
from loguru import logger


class GroqTranscriptionProvider:
    """
    Voice transcription provider using Groq's Whisper API.

    Groq offers extremely fast transcription with a generous free tier.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str = "whisper-large-v3",
        timeout_seconds: float = 60.0,
    ):
        self.api_key = api_key or os.environ.get("GROQ_API_KEY")
        self.api_url = "https://api.groq.com/openai/v1/audio/transcriptions"
        self.model = model
        self.timeout_seconds = timeout_seconds

    async def transcribe(self, file_path: str | Path) -> str:
        """
        Transcribe an audio file using Groq.

        Args:
            file_path: Path to the audio file.

        Returns:
            Transcribed text.
        """
        if not self.api_key:
            logger.warning("Groq API key not configured for transcription")
            return ""

        path = Path(file_path)
        if not path.exists():
            logger.error(f"Audio file not found: {file_path}")
            return ""

        try:
            async with httpx.AsyncClient() as client:
                with open(path, "rb") as f:
                    files = {
                        "file": (path.name, f),
                        "model": (None, self.model),
                    }
                    headers = {
                        "Authorization": f"Bearer {self.api_key}",
                    }

                    response = await client.post(
                        self.api_url,
                        headers=headers,
                        files=files,
                        timeout=self.timeout_seconds
                    )

                    response.raise_for_status()
                    data = response.json()
                    return data.get("text", "")

        except Exception as e:
            logger.error(f"Groq transcription error: {e}")
            return ""


class OpenAITranscriptionProvider:
    """Voice transcription provider using OpenAI's transcription API."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        api_base: str | None = None,
        extra_headers: dict[str, str] | None = None,
        model: str = "whisper-1",
        timeout_seconds: float = 60.0,
    ):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        base = api_base or os.environ.get("OPENAI_API_BASE") or "https://api.openai.com/v1"
        self.api_url = base.rstrip("/") + "/audio/transcriptions"
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.extra_headers = extra_headers

    async def transcribe(self, file_path: str | Path) -> str:
        if not self.api_key:
            logger.warning("OpenAI API key not configured for transcription")
            return ""

        path = Path(file_path)
        if not path.exists():
            logger.error(f"Audio file not found: {file_path}")
            return ""

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            **(self.extra_headers or {}),
        }
        models = [self.model]
        if "openrouter.ai" in self.api_url and "/" not in self.model:
            models.append(f"openai/{self.model}")

        response: httpx.Response | None = None
        try:
            async with httpx.AsyncClient() as client:
                for model_value in models:
                    with open(path, "rb") as f:
                        files = {
                            "file": (path.name, f),
                            "model": (None, model_value),
                        }
                        response = await client.post(
                            self.api_url,
                            headers=headers,
                            files=files,
                            timeout=self.timeout_seconds,
                        )
                    if response.status_code < 400:
                        data = response.json()
                        return data.get("text", "")
        except Exception as e:
            logger.error(f"OpenAI transcription error: {e}")
            return ""

        try:
            if response is None:
                return ""
            response.raise_for_status()
        except Exception as e:
            logger.error(f"OpenAI transcription error: {e}")
        return ""
