"""Voice transcription providers."""

import base64
import os
import shutil
import subprocess
import tempfile
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
        if "openrouter.ai" in self.api_url:
            return await self._transcribe_openrouter(path, headers=headers)

        models = [self.model]

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

    async def _transcribe_openrouter(self, path: Path, *, headers: dict[str, str]) -> str:
        chat_url = self.api_url.rsplit("/audio/transcriptions", 1)[0] + "/chat/completions"
        model_value = self._resolve_openrouter_model()
        audio_format, audio_bytes = self._prepare_openrouter_audio(path)
        if not audio_bytes:
            return ""

        payload = {
            "model": model_value,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Transcribe this audio exactly. "
                                "Return only transcript text without explanations."
                            ),
                        },
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": base64.b64encode(audio_bytes).decode("ascii"),
                                "format": audio_format,
                            },
                        },
                    ],
                }
            ],
            "temperature": 0,
        }

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    chat_url,
                    headers=headers,
                    json=payload,
                    timeout=self.timeout_seconds,
                )
            response.raise_for_status()
            data = response.json()
            return self._extract_chat_content(data)
        except Exception as e:
            logger.error(f"OpenRouter transcription error: {e}")
            return ""

    def _resolve_openrouter_model(self) -> str:
        model_value = str(self.model or "").strip()
        if not model_value:
            return "google/gemini-2.5-flash-lite"
        normalized = model_value.lower()
        if normalized in {"whisper-1", "whisper-large-v3"} or normalized.startswith("openai/whisper"):
            return "google/gemini-2.5-flash-lite"
        return model_value

    def _prepare_openrouter_audio(self, path: Path) -> tuple[str, bytes]:
        suffix = path.suffix.lower().lstrip(".")
        if suffix in {"wav", "mp3"}:
            try:
                return suffix, path.read_bytes()
            except OSError:
                return suffix, b""

        converted = self._convert_audio_to_wav(path)
        if converted:
            return "wav", converted

        try:
            fallback = path.read_bytes()
        except OSError:
            return suffix or "wav", b""
        return suffix or "wav", fallback

    def _convert_audio_to_wav(self, path: Path) -> bytes | None:
        ffmpeg_bin = shutil.which("ffmpeg")
        if not ffmpeg_bin:
            return None

        fd, tmp_name = tempfile.mkstemp(prefix="nanobot-asr-", suffix=".wav")
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            result = subprocess.run(
                [
                    ffmpeg_bin,
                    "-y",
                    "-i",
                    str(path),
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    str(tmp_path),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=False,
                timeout=max(5, int(self.timeout_seconds)),
            )
            if result.returncode != 0:
                return None
            return tmp_path.read_bytes()
        except Exception:
            return None
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _extract_chat_content(payload: dict) -> str:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        first = choices[0]
        if not isinstance(first, dict):
            return ""
        message = first.get("message")
        if not isinstance(message, dict):
            return ""
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
            return " ".join(parts).strip()
        return ""
