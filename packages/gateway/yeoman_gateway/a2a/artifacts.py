"""Private A2A voice artifact storage."""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import os
import re
import shutil
import subprocess
import time
import uuid
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
_CLEANUP_LIMIT = 64
_SIDECAR_MAX_BYTES = 16_384


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
        managed_outgoing_root: Path | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.root = root.expanduser().resolve()
        self.managed_outgoing_root = (
            managed_outgoing_root.expanduser().resolve()
            if managed_outgoing_root is not None
            else None
        )
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
        managed = self.managed_outgoing_root
        if (
            self.profile.kind != "tts"
            or managed is None
            or self.root == managed
            or not self.root.is_relative_to(managed)
            or not os.access(self.root, os.W_OK | os.X_OK)
            or not shutil.which("ffprobe")
        ):
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
        if not self.available:
            raise VoiceArtifactError("CAPABILITY_UNAVAILABLE")
        if not _EFFECT_ID.fullmatch(effect_id):
            raise VoiceArtifactError("INVALID_EFFECT_ID")
        self.cleanup_expired()
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
                provider_format, _, _ = _FORMATS[requested_format]
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

            temp = self.root / f".{effect_id}-{uuid.uuid4().hex}.tmp"
            try:
                descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(audio)
                    handle.flush()
                    os.fsync(handle.fileno())
                duration_ms, extension, mime_type = self._probe(temp)
                path = self.root / f"{effect_id}{extension}"
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

    def cleanup_expired(self, *, limit: int = _CLEANUP_LIMIT) -> int:
        """Delete a bounded number of expired artifacts owned by this store."""
        removed = 0
        candidates = itertools.islice(
            self.root.glob("a2a-effect-*.json"), max(0, int(limit))
        )
        for sidecar in candidates:
            effect_id = sidecar.stem
            if not _EFFECT_ID.fullmatch(effect_id) or sidecar.is_symlink():
                continue
            try:
                if sidecar.stat().st_size > _SIDECAR_MAX_BYTES:
                    continue
                value = json.loads(sidecar.read_text())
                if not isinstance(value, dict) or float(value["expires_at"]) > self.clock():
                    continue
                artifact = Path(str(value["path"]))
                filename = str(value["filename"])
                if (
                    artifact.parent != self.root
                    or artifact.name != filename
                    or not artifact.name.startswith(effect_id + ".")
                    or artifact.suffix not in {".ogg", ".mp3", ".wav"}
                    or artifact.is_symlink()
                ):
                    continue
                artifact.unlink(missing_ok=True)
                sidecar.unlink(missing_ok=True)
                removed += 1
            except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
        return removed

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
    def _probe(path: Path) -> tuple[int, str, str]:
        try:
            probe = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=format_name,duration:stream=codec_name,codec_type",
                    "-of",
                    "json",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            value = json.loads(probe.stdout) if not probe.returncode else {}
            streams = value.get("streams", [])
            audio_streams = [
                item for item in streams if isinstance(item, dict) and item.get("codec_type") == "audio"
            ]
            if len(audio_streams) != 1:
                raise ValueError
            codec = str(audio_streams[0].get("codec_name") or "").lower()
            raw_format = str(value.get("format", {}).get("format_name") or "").lower()
            duration = round(float(value.get("format", {}).get("duration")) * 1000)
        except (
            OSError,
            subprocess.SubprocessError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise VoiceArtifactError("INVALID_AUDIO") from exc
        if duration < 1:
            raise VoiceArtifactError("INVALID_AUDIO")
        formats = set(raw_format.split(","))
        if "ogg" in formats and codec == "opus":
            return duration, ".ogg", "audio/ogg; codecs=opus"
        if "mp3" in formats and codec == "mp3":
            return duration, ".mp3", "audio/mpeg"
        if "wav" in formats and codec.startswith(("pcm_", "adpcm_")):
            return duration, ".wav", "audio/wav"
        raise VoiceArtifactError("INVALID_AUDIO")
