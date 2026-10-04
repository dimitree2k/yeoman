"""Vision capability executor for image and video description."""

from __future__ import annotations

import asyncio
import base64
import mimetypes
from pathlib import Path

from loguru import logger

from yeoman_gateway.media.router import ResolvedProfile
from yeoman_gateway.providers.factory import ProviderFactory

PROMPT = (
    "Describe the image in 3-6 factual sentences, using fewer sentences for simple images "
    "and more when the image supports useful detail. Start with the overall scene, then "
    "describe important objects and their actions or relationships. Include notable "
    "background details when they add context. Quote short visible text exactly when it "
    "is clearly readable. Mark uncertainty and do not infer identities or unseen context."
)

VIDEO_PROMPT = (
    "Describe the complete video in 1-2 concise factual sentences. "
    "Use visible motion and audible speech or sounds when relevant. "
    "Do not invent details or infer anything that is not clearly visible or audible."
)

OCR_PROMPT = (
    "Transcribe the visible text in this image as accurately as possible. "
    "Preserve line breaks when useful. If the image contains no readable visible text, "
    "reply with an empty string. Do not describe non-text visual elements unless needed "
    "to disambiguate the text."
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

    async def ocr_image(self, image_path: Path, profile: ResolvedProfile) -> str | None:
        """Extract visible text from a local image using a routed vision/OCR model."""
        if profile.kind not in {"vision", "ocr"} or not profile.model:
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
                    {"type": "text", "text": OCR_PROMPT},
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
                    max_tokens=profile.max_tokens or 1200,
                    temperature=profile.temperature if profile.temperature is not None else 0.0,
                ),
                timeout=max(1.0, timeout_s),
            )
        except Exception:
            return None
        text = (response.content or "").strip()
        if not text:
            return None
        return text

    async def describe_video(
        self,
        video_path: Path,
        profile: ResolvedProfile,
    ) -> str | None:
        """Send the complete local video to a video-capable routed model."""
        if profile.kind != "vision" or not profile.model:
            return None
        if not video_path.exists() or not video_path.is_file():
            return None

        mime, _ = mimetypes.guess_type(str(video_path))
        mime = {
            ".m4v": "video/mp4",
            ".mov": "video/mov",
        }.get(video_path.suffix.lower(), mime)
        if mime not in {"video/mp4", "video/mpeg", "video/mov", "video/webm"}:
            logger.debug("Video description skipped for unsupported MIME type {}", mime)
            return None
        b64 = base64.b64encode(video_path.read_bytes()).decode("ascii")

        try:
            content = [
                {
                    "type": "video_url",
                    "video_url": {"url": f"data:{mime};base64,{b64}"},
                },
                {"type": "text", "text": VIDEO_PROMPT},
            ]
            provider = self._provider_factory.create_chat_provider(
                profile.model,
                profile.provider,
            )
            messages = [{"role": "user", "content": content}]

            timeout_s = max((profile.timeout_ms or 0) / 1000.0, 60.0)
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
