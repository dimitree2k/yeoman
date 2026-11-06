"""Channel-agnostic video understanding: a local file or a video link in, text out.

One service serves every caller (WhatsApp, Telegram, the ``video_understand`` tool).
The audio track goes to the routed speech-to-text model (``asr.transcribe_audio``) first;
then the whole clip, timestamped stills and the caption + transcript go to the routed
video model (``vision.describe_video``), which explains the point (or punchline) before
the details. Links are fetched with yt-dlp at low resolution first; the post's own
caption and title come from the same metadata call.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import tempfile
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from yeoman_gateway.media.asr import ASRTranscriber
    from yeoman_gateway.media.router import ModelRouter
    from yeoman_gateway.media.vision import VisionDescriber

#: Clips longer than this are not downloaded from links (reels and shorts fit easily).
DEFAULT_MAX_URL_DURATION_SECONDS = 180
DEFAULT_MAX_DOWNLOAD_BYTES = 40 * 1024 * 1024
#: The video model gets the file inline as base64; bigger files get audio + caption only.
DEFAULT_MAX_DESCRIBE_BYTES = 20 * 1024 * 1024
_MAX_CAPTION_CHARS = 1500
_MAX_TRANSCRIPT_CHARS = 12000
#: Descriptions land in every reader's context; reasoning models ignore length requests.
_MAX_DESCRIPTION_CHARS = 1800
#: Stills sent next to the video: providers sample clips sparsely and miss sub-second
#: moments (a person crossing the hood at 4 s was invisible to every model without them).
_MAX_FRAMES = 24
_FRAMES_PER_SECOND = 2.0
_FRAME_WIDTH = 432
_FFMPEG_TIMEOUT_SECONDS = 120
_ASR_MIN_TIMEOUT_MS = 120_000
_CACHE_SIZE = 64
_CACHE_TTL_SECONDS = 6 * 60 * 60
#: Low resolution keeps the upload small; combined audio+video formats come first.
_YTDLP_FORMAT = (
    "best[height<=480][vcodec!=none][acodec!=none]"
    "/worst[vcodec!=none][acodec!=none]"
    "/best[height<=480]/worst"
)


class VideoUnderstandingError(Exception):
    """User-facing reason a video link could not be understood."""


@dataclass(frozen=True, slots=True)
class VideoInsight:
    """What could be learned about one video. Every field may be missing."""

    description: str | None = None
    transcript: str | None = None
    duration_s: float | None = None
    description_model: str | None = None
    transcript_model: str | None = None
    # Link metadata (empty for local files).
    source_url: str | None = None
    site: str | None = None
    title: str | None = None
    caption: str | None = None
    uploader: str | None = None

    @property
    def empty(self) -> bool:
        return not (self.description or self.transcript or self.caption or self.title)

    def enrichment_lines(self) -> list[str]:
        """Tagged lines appended to an inbound message's text."""
        lines: list[str] = []
        if self.description:
            lines.append(f"[video_description] {self.description}")
        if self.transcript:
            lines.append(f"[video_transcript] {self.transcript}")
        return lines

    def as_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value not in (None, "")}


class _TTLCache:
    def __init__(self, size: int = _CACHE_SIZE, ttl_s: float = _CACHE_TTL_SECONDS) -> None:
        self._items: OrderedDict[str, tuple[float, VideoInsight]] = OrderedDict()
        self._size = size
        self._ttl = ttl_s

    def get(self, key: str) -> VideoInsight | None:
        item = self._items.get(key)
        if item is None:
            return None
        stored, value = item
        if time.monotonic() - stored > self._ttl:
            self._items.pop(key, None)
            return None
        self._items.move_to_end(key)
        return value

    def put(self, key: str, value: VideoInsight) -> None:
        self._items[key] = (time.monotonic(), value)
        self._items.move_to_end(key)
        while len(self._items) > self._size:
            self._items.popitem(last=False)


def _clip(text: str | None, limit: int) -> str | None:
    if not text:
        return None
    text = " ".join(str(text).split())
    if not text:
        return None
    return text if len(text) <= limit else text[:limit] + " [truncated]"


def _clip_sentences(text: str | None, limit: int) -> str | None:
    """Whitespace-normalized text (line breaks kept), cut at a sentence end within ``limit``."""
    if not text:
        return None
    lines = (" ".join(line.split()) for line in str(text).splitlines())
    text = "\n".join(line for line in lines if line)
    if len(text) <= limit:
        return text or None
    head = text[:limit]
    cut = max(
        head.rfind(". "), head.rfind("! "), head.rfind("? "), head.rfind('." '),
        head.rfind(".\n"), head.rfind("!\n"), head.rfind("?\n"),
    )
    if cut >= limit // 2:
        return head[: cut + 1]
    return head.rsplit(" ", 1)[0] + " …"


async def _run(argv: list[str], timeout_s: float) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode or 0, out, err


async def probe_video(path: Path) -> tuple[float | None, bool]:
    """(duration in seconds, has an audio stream). Unknown without ffprobe."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None, True
    try:
        code, out, _ = await _run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration:stream=codec_type",
             "-of", "json", str(path)],
            30,
        )
    except (OSError, TimeoutError):
        return None, True
    if code != 0:
        return None, True
    try:
        data = json.loads(out.decode() or "{}")
    except ValueError:
        return None, True
    streams = data.get("streams") or []
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    try:
        duration = float((data.get("format") or {}).get("duration"))
    except (TypeError, ValueError):
        duration = None
    return duration, has_audio


async def extract_audio(source: Path, target: Path) -> bool:
    """Mono 16 kHz 32 kbit/s MP3, the shape speech-to-text uploads want."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        logger.warning("video understanding: ffmpeg not installed; no transcript")
        return False
    try:
        code, _, err = await _run(
            [ffmpeg, "-nostdin", "-y", "-i", str(source), "-vn", "-ac", "1", "-ar", "16000",
             "-b:a", "32k", str(target)],
            _FFMPEG_TIMEOUT_SECONDS,
        )
    except (OSError, TimeoutError) as exc:
        logger.warning("video understanding: audio extraction failed {}", type(exc).__name__)
        return False
    if code != 0 or not target.is_file() or target.stat().st_size == 0:
        logger.warning("video understanding: ffmpeg failed: {}", err.decode(errors="replace")[-300:])
        return False
    return True


async def extract_frames(source: Path, target_dir: Path, duration: float | None) -> list[Path]:
    """Up to ``_MAX_FRAMES`` evenly spaced, timestamped stills (empty without ffmpeg)."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return []
    fps = _FRAMES_PER_SECOND
    if duration and duration * fps > _MAX_FRAMES:
        fps = _MAX_FRAMES / duration
    scale = f"fps={fps:.4f},scale={_FRAME_WIDTH}:-2"
    stamp = (
        ",drawtext=text='%{pts\\:hms}':x=6:y=6:fontsize=20:fontcolor=yellow"
        ":box=1:boxcolor=black@0.6"
    )
    pattern = str(target_dir / "frame_%02d.jpg")
    # drawtext needs a font; without one the frames still help, just without a clock.
    for filters in (scale + stamp, scale):
        try:
            code, _, _ = await _run(
                [ffmpeg, "-nostdin", "-v", "error", "-y", "-i", str(source), "-vf", filters,
                 "-frames:v", str(_MAX_FRAMES), "-q:v", "4", pattern],
                _FFMPEG_TIMEOUT_SECONDS,
            )
        except (OSError, TimeoutError):
            return []
        frames = sorted(target_dir.glob("frame_*.jpg"))
        if code == 0 and frames:
            return frames
        for frame in frames:
            frame.unlink(missing_ok=True)
    return []


async def shrink_video(source: Path, target: Path) -> bool:
    """Re-encode to <=480p at a low bitrate so a long or sharp clip fits inline."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    try:
        code, _, err = await _run(
            [ffmpeg, "-nostdin", "-v", "error", "-y", "-i", str(source),
             "-vf", "scale=-2:'min(480,ih)'", "-c:v", "libx264", "-preset", "veryfast",
             "-crf", "32", "-c:a", "aac", "-b:a", "48k", "-movflags", "+faststart",
             str(target)],
            _FFMPEG_TIMEOUT_SECONDS,
        )
    except (OSError, TimeoutError):
        return False
    if code != 0 or not target.is_file():
        logger.warning("video understanding: shrink failed: {}", err.decode(errors="replace")[-300:])
        return False
    return True


def _context_text(caption: str | None, transcript: str | None) -> str | None:
    parts = []
    if caption:
        parts.append(f"Caption: {caption}")
    if transcript:
        parts.append(f"Speech in the video (automatic transcript, may contain errors): {transcript}")
    return "\n".join(parts) or None


def _download_reason(exc: Exception) -> str:
    text = str(exc).removeprefix("ERROR: ").strip()
    first = text.split(". ")[0].split(";")[0]
    return first[:220] or exc.__class__.__name__


class VideoUnderstanding:
    """Describe and transcribe videos for any channel or tool."""

    def __init__(
        self,
        *,
        describer: "VisionDescriber | None",
        router: "ModelRouter | None",
        transcriber: "ASRTranscriber | None",
        cookies_file: str | None = None,
        max_url_duration_s: int = DEFAULT_MAX_URL_DURATION_SECONDS,
        max_download_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES,
        max_describe_bytes: int = DEFAULT_MAX_DESCRIBE_BYTES,
    ) -> None:
        self._describer = describer
        self._router = router
        self._transcriber = transcriber
        self._cookies_file = cookies_file or None
        self._max_url_duration_s = max_url_duration_s
        self._max_download_bytes = max_download_bytes
        self._max_describe_bytes = max_describe_bytes
        self._file_cache = _TTLCache()
        self._url_cache = _TTLCache()
        self._url_locks: dict[str, asyncio.Lock] = {}

    @property
    def describer(self) -> "VisionDescriber | None":
        """The video model executor (exposed so owner failure alerts can be attached)."""
        return self._describer

    # -- local files -------------------------------------------------------------------

    async def understand_file(
        self, path: Path, *, channel: str | None = None, caption: str | None = None
    ) -> VideoInsight:
        """Transcribe the audio, then describe the clip with caption and speech as context.

        The punchline of a clip often sits in its caption or in one spoken line, so the
        video model sees both. Speech-to-text takes a few seconds; frames are cut meanwhile.
        """
        try:
            data_hash = await asyncio.to_thread(_sha256, path)
        except OSError:
            return VideoInsight()
        cache_key = f"{data_hash}:{caption or ''}"
        cached = self._file_cache.get(cache_key)
        if cached is not None:
            return cached
        duration, has_audio = await probe_video(path)
        with tempfile.TemporaryDirectory(prefix="yeoman-frames-") as tmp:
            transcript, frames = await asyncio.gather(
                self._transcribe(path, channel) if has_audio else _none(),
                extract_frames(path, Path(tmp), duration),
            )
            context = _context_text(caption, transcript[0] if transcript else None)
            description = await self._describe(path, channel, frames=frames, context=context)
        insight = VideoInsight(
            description=description[0] if description else None,
            description_model=description[1] if description else None,
            transcript=transcript[0] if transcript else None,
            transcript_model=transcript[1] if transcript else None,
            duration_s=round(duration, 1) if duration else None,
        )
        if insight.description or insight.transcript:
            self._file_cache.put(cache_key, insight)
        return insight

    async def _describe(
        self,
        path: Path,
        channel: str | None,
        *,
        frames: list[Path] | None = None,
        context: str | None = None,
    ) -> tuple[str, str | None] | None:
        if self._describer is None or self._router is None:
            return None
        try:
            size = path.stat().st_size
        except OSError:
            return None
        try:
            profile = self._router.resolve("vision.describe_video", channel=channel)
        except KeyError as exc:
            logger.warning("video understanding: no video route ({})", exc)
            return None
        with tempfile.TemporaryDirectory(prefix="yeoman-shrink-") as tmp:
            if size > self._max_describe_bytes:
                # The clip goes inline as base64; re-encode instead of going blind.
                small = Path(tmp) / "small.mp4"
                if not await shrink_video(path, small) or (
                    small.stat().st_size > self._max_describe_bytes
                ):
                    logger.info(
                        "video understanding: description skipped, file too large bytes={}",
                        size,
                    )
                    return None
                path = small
            try:
                text = await self._describer.describe_video(
                    path, profile, frames=frames or (), context=context
                )
            except Exception as exc:  # noqa: BLE001 - a failed description must not drop the turn
                logger.warning(
                    "video understanding: description failed {}", type(exc).__name__
                )
                return None
        text = _clip_sentences(text, _MAX_DESCRIPTION_CHARS)
        return (text, profile.model) if text else None

    async def _transcribe(self, path: Path, channel: str | None) -> tuple[str, str | None] | None:
        if self._transcriber is None or self._router is None:
            return None
        try:
            profile = self._router.resolve("asr.transcribe_audio", channel=channel)
        except KeyError as exc:
            logger.warning("video understanding: no speech-to-text route ({})", exc)
            return None
        profile = replace(profile, timeout_ms=max(profile.timeout_ms or 0, _ASR_MIN_TIMEOUT_MS))
        with tempfile.TemporaryDirectory(prefix="yeoman-video-") as tmp:
            audio = Path(tmp) / "audio.mp3"
            if not await extract_audio(path, audio):
                return None
            try:
                result = await self._transcriber.transcribe(audio, profile)
            except Exception as exc:  # noqa: BLE001
                logger.warning("video understanding: transcription failed {}", type(exc).__name__)
                return None
        text = _clip(result.text if result else None, _MAX_TRANSCRIPT_CHARS)
        if not text:
            return None
        return text, (result.model if result is not None else None) or profile.model

    # -- links -------------------------------------------------------------------------

    async def understand_url(self, url: str, *, channel: str | None = None) -> VideoInsight:
        """Fetch a video link at low resolution, then understand it like a local file."""
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            raise VideoUnderstandingError("Not a web link.")
        cached = self._url_cache.get(url)
        if cached is not None:
            return cached
        if len(self._url_locks) > _CACHE_SIZE:
            # Forget idle locks; one in use stays so its waiters share the result.
            for key in [k for k, v in self._url_locks.items() if not v.locked()]:
                del self._url_locks[key]
        # Two messages with the same link wait for one download instead of starting two.
        lock = self._url_locks.setdefault(url, asyncio.Lock())
        async with lock:
            cached = self._url_cache.get(url)
            if cached is not None:
                return cached
            insight = await self._understand_url_uncached(url, channel)
            if not insight.empty:
                self._url_cache.put(url, insight)
            return insight

    async def _understand_url_uncached(self, url: str, channel: str | None) -> VideoInsight:
        with tempfile.TemporaryDirectory(prefix="yeoman-videolink-") as tmp:
            info, path = await asyncio.to_thread(self._download, url, Path(tmp))
            meta = {
                "source_url": info.get("webpage_url") or url,
                "site": info.get("extractor_key") or info.get("extractor"),
                "title": _clip(info.get("title"), 300),
                "caption": _clip(info.get("description"), _MAX_CAPTION_CHARS),
                "uploader": _clip(info.get("uploader") or info.get("channel"), 120),
            }
            if path is None:
                return VideoInsight(duration_s=info.get("duration"), **meta)
            local = await self.understand_file(path, channel=channel, caption=meta["caption"])
        logger.info(
            "video understanding url site={} duration={} description={} transcript={}",
            meta["site"], local.duration_s, bool(local.description), bool(local.transcript),
        )
        return VideoInsight(
            description=local.description,
            transcript=local.transcript,
            duration_s=local.duration_s or info.get("duration"),
            description_model=local.description_model,
            transcript_model=local.transcript_model,
            **meta,
        )

    def _ydl_options(self, target_dir: Path) -> dict[str, Any]:
        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "noprogress": True,
            "format": _YTDLP_FORMAT,
            "outtmpl": str(target_dir / "video.%(ext)s"),
            "max_filesize": self._max_download_bytes,
            "socket_timeout": 30,
            "retries": 2,
        }
        if self._cookies_file:
            options["cookiefile"] = self._cookies_file
        return options

    def _download(self, url: str, target_dir: Path) -> tuple[dict[str, Any], Path | None]:
        from yt_dlp import YoutubeDL
        from yt_dlp.utils import DownloadError

        try:
            with YoutubeDL(self._ydl_options(target_dir)) as ydl:
                info = ydl.extract_info(url, download=False) or {}
                if info.get("_type") == "playlist":
                    entries = [e for e in info.get("entries") or [] if e]
                    if not entries:
                        raise VideoUnderstandingError("The link has no video.")
                    info = entries[0]
                if info.get("is_live"):
                    raise VideoUnderstandingError("Live streams cannot be watched.")
                duration = int(info.get("duration") or 0)
                if duration > self._max_url_duration_s:
                    logger.info("video understanding: too long for download duration={}", duration)
                    return info, None
                ydl.process_info(info)
        except DownloadError as exc:
            raise VideoUnderstandingError(f"Could not fetch the video: {_download_reason(exc)}") from exc
        files = sorted(p for p in target_dir.glob("video.*") if not p.name.endswith(".part"))
        return info, (files[0] if files else None)


async def _none() -> None:
    return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
