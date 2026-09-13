"""Private A2A voice artifact storage."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import time
import uuid
import wave
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from yeoman_gateway.media.router import ResolvedProfile
from yeoman_gateway.media.tts import TTSSynthesizer

_EFFECT_ID = re.compile(r"^a2a-effect-[a-f0-9]{40}$")
_FORMATS = {
    "ogg_opus": ("opus", ".ogg", "audio/ogg; codecs=opus"),
    "mp3": ("mp3", ".mp3", "audio/mpeg"),
    "wav": ("wav", ".wav", "audio/wav"),
}


class VoiceArtifactError(RuntimeError):
    """A sanitized generation or storage failure."""


class VoiceArtifactStore:
    """Generate and retain deterministic private audio artifacts by effect id."""

    def __init__(
        self,
        root: Path,
        *,
        tts: TTSSynthesizer,
        profile: ResolvedProfile,
        max_bytes: int,
        ttl_seconds: int,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.tts = tts
        self.profile = profile
        self.max_bytes = max(1, int(max_bytes))
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.clock = clock or time.time
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def available(self) -> bool:
        if self.profile.kind != "tts" or not os.access(self.root, os.W_OK | os.X_OK):
            return False
        provider = (self.profile.provider or "openai_tts").strip().lower()
        if provider in {"", "openai_tts"}:
            return bool(self.tts._openai_api_key)
        if provider in {"elevenlabs_tts", "elevenlabs"}:
            return bool(
                self.tts._elevenlabs_api_key and self.tts._elevenlabs_default_voice_id
            )
        if provider == "openrouter_audio":
            return bool(
                self.tts._openrouter_api_key
                and shutil.which("ffmpeg")
                and shutil.which("ffprobe")
            )
        if provider == "fish_audio":
            return bool(self.tts._fish_api_key and self.tts._fish_default_voice_id)
        return False

    async def generate(
        self, effect_id: str, request: Mapping[str, Any]
    ) -> dict[str, object]:
        if not _EFFECT_ID.fullmatch(effect_id):
            raise VoiceArtifactError("INVALID_EFFECT_ID")
        request_hash = hashlib.sha256(
            json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        async with self._locks.setdefault(effect_id, asyncio.Lock()):
            existing = self._load(effect_id)
            if existing is not None:
                if existing.pop("request_hash") != request_hash:
                    raise VoiceArtifactError("ARTIFACT_CONFLICT")
                if float(existing["expires_at"]) > self.clock() and self._matches(existing):
                    return existing

            requested_format = str(request.get("format") or "ogg_opus")
            try:
                provider_format, extension, mime_type = _FORMATS[requested_format]
            except KeyError as exc:
                raise VoiceArtifactError("INVALID_FORMAT") from exc
            text = str(request.get("text") or "")
            voice = str(request.get("voice") or "alloy")
            try:
                audio, error = await self.tts.synthesize_with_status(
                    text,
                    profile=self.profile,
                    voice=voice,
                    format=provider_format,
                )
            except Exception as exc:
                raise VoiceArtifactError("TTS_FAILED") from exc
            if not audio:
                raise VoiceArtifactError(f"TTS_FAILED:{error or 'empty_audio'}")
            if len(audio) > self.max_bytes:
                raise VoiceArtifactError("ARTIFACT_TOO_LARGE")

            path = self.root / f"{effect_id}{extension}"
            temp = self.root / f".{effect_id}-{uuid.uuid4().hex}.tmp"
            try:
                descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(audio)
                    handle.flush()
                    os.fsync(handle.fileno())
                duration_ms = self._duration_ms(temp, requested_format)
                temp.replace(path)
                path.chmod(0o600)
            except VoiceArtifactError:
                temp.unlink(missing_ok=True)
                raise
            except OSError as exc:
                temp.unlink(missing_ok=True)
                raise VoiceArtifactError("ARTIFACT_WRITE_FAILED") from exc

            metadata: dict[str, object] = {
                "path": str(path),
                "mime_type": mime_type,
                "duration_ms": duration_ms,
                "sha256": hashlib.sha256(audio).hexdigest(),
                "size_bytes": len(audio),
                "expires_at": self.clock() + self.ttl_seconds,
                "filename": path.name,
            }
            self._save(effect_id, {"request_hash": request_hash, **metadata})
            return metadata

    def _sidecar(self, effect_id: str) -> Path:
        return self.root / f"{effect_id}.json"

    def _load(self, effect_id: str) -> dict[str, Any] | None:
        try:
            value = json.loads(self._sidecar(effect_id).read_text())
        except (OSError, json.JSONDecodeError):
            return None
        required = {
            "request_hash",
            "path",
            "mime_type",
            "duration_ms",
            "sha256",
            "size_bytes",
            "expires_at",
            "filename",
        }
        return value if isinstance(value, dict) and set(value) == required else None

    def _save(self, effect_id: str, value: Mapping[str, object]) -> None:
        path = self._sidecar(effect_id)
        temp = self.root / f".{effect_id}-{uuid.uuid4().hex}.json"
        try:
            descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(value, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            temp.replace(path)
            path.chmod(0o600)
        except OSError as exc:
            temp.unlink(missing_ok=True)
            raise VoiceArtifactError("ARTIFACT_WRITE_FAILED") from exc

    def _matches(self, metadata: Mapping[str, Any]) -> bool:
        path = Path(str(metadata["path"]))
        try:
            return (
                path.parent == self.root
                and not path.is_symlink()
                and path.is_file()
                and path.stat().st_size == int(metadata["size_bytes"])
                and hashlib.sha256(path.read_bytes()).hexdigest() == metadata["sha256"]
            )
        except OSError, ValueError, TypeError:
            return False

    @staticmethod
    def _duration_ms(path: Path, format: str) -> int:
        if format == "wav":
            try:
                with wave.open(io.BytesIO(path.read_bytes()), "rb") as audio:
                    frames = audio.getnframes()
                    rate = audio.getframerate()
                duration = round(frames * 1000 / rate) if rate else 0
            except (OSError, EOFError, wave.Error):
                duration = 0
        else:
            try:
                probe = subprocess.run(
                    [
                        "ffprobe",
                        "-v",
                        "error",
                        "-show_entries",
                        "format=duration",
                        "-of",
                        "default=noprint_wrappers=1:nokey=1",
                        str(path),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                duration = round(float(probe.stdout.strip()) * 1000) if not probe.returncode else 0
            except (OSError, subprocess.SubprocessError, ValueError):
                duration = 0
        if duration < 1:
            raise VoiceArtifactError("INVALID_AUDIO")
        return duration
