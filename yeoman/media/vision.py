"""Vision capability executor for image and video description."""

from __future__ import annotations

import asyncio
import base64
import mimetypes
import shutil
import tempfile
from pathlib import Path

from loguru import logger

from yeoman.media.router import ResolvedProfile
from yeoman.providers.factory import ProviderFactory

PROMPT = (
    "Describe this image in 1-2 concise sentences. "
    "Be factual, include key objects/action, and mention visible text only if readable."
)

VIDEO_PROMPT = (
    "These are frames extracted from a short video, in chronological order. "
    "Describe what is happening in 1-2 concise sentences. "
    "Be factual, include key objects/actions, and mention visible text only if readable."
)


class VisionDescriber:
    """Describe local image files using a routed vision-capable model."""

    def __init__(self, provider_factory: ProviderFactory) -> None:
        self._provider_factory = provider_factory

    async def describe(self, image_path: Path, profile: ResolvedProfile) -> str | None:
        if profile.kind != "vision" or not profile.model:
            return None
        if not image_path.exists() or not image_path.is_file():
            return None

        mime, _ = mimetypes.guess_type(str(image_path))
        if not mime or not mime.startswith("image/"):
            return None

        b64 = base64.b64encode(image_path.read_bytes()).decode()
        provider = self._provider_factory.create_chat_provider(profile.model)
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ],
            }
        ]

        timeout_s = (profile.timeout_ms or 12000) / 1000.0
        try:
            response = await asyncio.wait_for(
                provider.chat(
                    messages=messages,
                    model=profile.model,
                    max_tokens=profile.max_tokens or 160,
                    temperature=profile.temperature if profile.temperature is not None else 0.1,
                ),
                timeout=max(1.0, timeout_s),
            )
        except Exception:
            return None
        text = (response.content or "").strip()
        if not text:
            return None
        return " ".join(text.split())

    async def describe_video(
        self,
        video_path: Path,
        profile: ResolvedProfile,
        frame_count: int = 4,
    ) -> str | None:
        """Extract frames from a video and describe them in a single vision API call."""
        if profile.kind != "vision" or not profile.model:
            return None
        if not video_path.exists() or not video_path.is_file():
            return None

        frames = await self._extract_frames(video_path, frame_count)
        if not frames:
            return None

        try:
            content: list[dict] = [{"type": "text", "text": VIDEO_PROMPT}]
            for frame_path in frames:
                b64 = base64.b64encode(frame_path.read_bytes()).decode()
                content.append(
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
                )

            provider = self._provider_factory.create_chat_provider(profile.model)
            messages = [{"role": "user", "content": content}]

            timeout_s = (profile.timeout_ms or 20000) / 1000.0
            response = await asyncio.wait_for(
                provider.chat(
                    messages=messages,
                    model=profile.model,
                    max_tokens=profile.max_tokens or 320,
                    temperature=profile.temperature if profile.temperature is not None else 0.1,
                ),
                timeout=max(1.0, timeout_s),
            )
            text = (response.content or "").strip()
            if not text:
                return None
            return " ".join(text.split())
        except Exception:
            logger.opt(exception=True).debug("Video description failed for {}", video_path)
            return None
        finally:
            for f in frames:
                f.unlink(missing_ok=True)
            # Clean up temp directory if all frames came from the same one
            if frames and frames[0].parent != video_path.parent:
                shutil.rmtree(frames[0].parent, ignore_errors=True)

    @staticmethod
    async def _extract_frames(video_path: Path, count: int) -> list[Path]:
        """Extract *count* evenly-spaced frames from *video_path* using ffmpeg."""
        if not shutil.which("ffmpeg"):
            logger.warning("ffmpeg not found — cannot extract video frames")
            return []

        tmpdir = Path(tempfile.mkdtemp(prefix="yeoman_vframes_"))
        # Use ffmpeg thumbnail filter to pick representative frames, falling back to
        # uniform time-based selection via fps filter with total frame count limit.
        # The select filter picks every Nth frame; we probe duration first.
        try:
            probe = await asyncio.create_subprocess_exec(
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(video_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(probe.communicate(), timeout=10)
            duration = float(stdout.decode().strip() or "0")
        except Exception:
            duration = 0.0

        if duration <= 0:
            # Fallback: just grab the first frame
            count = 1
            duration = 1.0

        # Extract frames at evenly spaced timestamps
        frames: list[Path] = []
        interval = duration / count
        for i in range(count):
            ts = interval * i + interval / 2  # middle of each segment
            ts = min(ts, max(0, duration - 0.1))
            out_path = tmpdir / f"frame_{i:03d}.jpg"
            try:
                proc = await asyncio.create_subprocess_exec(
                    "ffmpeg",
                    "-ss", f"{ts:.3f}",
                    "-i", str(video_path),
                    "-frames:v", "1",
                    "-q:v", "2",
                    "-y",
                    str(out_path),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(proc.communicate(), timeout=15)
                if out_path.exists() and out_path.stat().st_size > 0:
                    frames.append(out_path)
            except Exception:
                logger.opt(exception=True).debug("Frame extraction failed at ts={}", ts)

        if not frames:
            shutil.rmtree(tmpdir, ignore_errors=True)
        return frames
