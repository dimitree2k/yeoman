"""Speech-to-text fallback for YouTube videos without captions (yt-dlp + routed ASR)."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from loguru import logger

#: Transcribes one local audio file; returns the text or None.
AudioTranscriber = Callable[[Path], Awaitable[str | None]]

MAX_DURATION_SECONDS = 90 * 60
#: Whisper-style upload APIs cap files at 25 MB; 32 kbit/s mono keeps 90 min under it.
_MAX_UPLOAD_BYTES = 24 * 1024 * 1024
_MAX_DOWNLOAD_BYTES = 300 * 1024 * 1024
_FFMPEG_TIMEOUT_SECONDS = 300


class YoutubeAudioError(Exception):
    """User-facing reason the audio fallback could not produce a transcript."""


def _ydl_options(target_dir: Path) -> dict[str, Any]:
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "noprogress": True,
        "format": "bestaudio[abr<=96]/bestaudio/worst",
        "outtmpl": str(target_dir / "source.%(ext)s"),
        "max_filesize": _MAX_DOWNLOAD_BYTES,
        "socket_timeout": 30,
        "retries": 2,
    }


def _download_audio(url: str, target_dir: Path, max_duration: int) -> Path:
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError

    try:
        with YoutubeDL(_ydl_options(target_dir)) as ydl:
            info = ydl.extract_info(url, download=False) or {}
            if info.get("is_live"):
                raise YoutubeAudioError("Live streams cannot be transcribed.")
            duration = int(info.get("duration") or 0)
            if duration > max_duration:
                raise YoutubeAudioError(
                    f"Video is {duration // 60} min long; audio transcription is limited to "
                    f"{max_duration // 60} min."
                )
            ydl.process_info(info)
    except DownloadError as exc:
        reason = str(exc).removeprefix("ERROR: ").split(";")[0][:200]
        raise YoutubeAudioError(f"Could not download the audio: {reason}") from exc
    files = sorted(target_dir.glob("source.*"))
    if not files:
        raise YoutubeAudioError("Could not download the audio.")
    return files[0]


async def _compress(source: Path, target: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise YoutubeAudioError("ffmpeg is not installed on the host.")
    proc = await asyncio.create_subprocess_exec(
        ffmpeg, "-nostdin", "-y", "-i", str(source), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k",
        str(target),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_FFMPEG_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise YoutubeAudioError("Audio conversion timed out.") from exc
    if proc.returncode != 0 or not target.is_file():
        logger.warning("youtube audio ffmpeg failed: {}", stderr.decode(errors="replace")[-400:])
        raise YoutubeAudioError("Audio conversion failed.")


async def transcribe_youtube_audio(
    url: str,
    transcriber: AudioTranscriber,
    *,
    max_duration: int = MAX_DURATION_SECONDS,
) -> str:
    """Download a video's audio, shrink it for upload and transcribe it."""
    with tempfile.TemporaryDirectory(prefix="yeoman-yt-") as tmp:
        tmp_dir = Path(tmp)
        source = await asyncio.to_thread(_download_audio, url, tmp_dir, max_duration)
        audio = tmp_dir / "audio.mp3"
        await _compress(source, audio)
        if audio.stat().st_size > _MAX_UPLOAD_BYTES:
            raise YoutubeAudioError("Audio is too large to transcribe.")
        text = await transcriber(audio)
    if not text or not text.strip():
        raise YoutubeAudioError("Speech-to-text returned no text.")
    return " ".join(text.split())
