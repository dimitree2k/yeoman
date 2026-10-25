"""Vision capability executor for image and video description."""

from __future__ import annotations

import asyncio
import base64
import mimetypes
from collections.abc import Callable
from pathlib import Path

from loguru import logger

from yeoman_gateway.media.router import ModelRouter, ResolvedProfile
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

    def __init__(
        self, provider_factory: ProviderFactory, router: ModelRouter | None = None
    ) -> None:
        self._provider_factory = provider_factory
        # Resolves a profile's `fallback` names; without it only the primary is tried.
        self._router = router
        # Called with (task, profile, reason) when every model in the chain failed,
        # timed out or returned nothing, so the owner hears about media that went unseen.
        self.failure_listener: Callable[[str, ResolvedProfile, str], None] | None = None

    def _report_failure(self, task: str, profile: ResolvedProfile, reason: str) -> None:
        if self.failure_listener is not None:
            try:
                self.failure_listener(task, profile, reason)
            except Exception:
                logger.opt(exception=True).debug("vision failure listener raised")

    def _chain(self, profile: ResolvedProfile) -> list[ResolvedProfile]:
        chain = [profile]
        if self._router is None:
            return chain
        for name in profile.fallback:
            try:
                chain.append(self._router.resolve_by_profile(name))
            except KeyError:
                logger.warning("vision fallback profile missing name={}", name)
        return chain

    async def _run(
        self,
        task: str,
        profile: ResolvedProfile,
        content: list[dict[str, object]],
        *,
        timeout_s: float,
        default_max_tokens: int,
        default_temperature: float,
        empty_is_failure: bool = True,
    ) -> str | None:
        """Try the profile, then its fallbacks; return the first non-empty answer."""
        reasons: list[str] = []
        chain = self._chain(profile)
        for candidate in chain:
            if not candidate.model:
                continue
            timeout = max(1.0, timeout_s)
            try:
                provider = self._provider_factory.create_chat_provider(
                    candidate.model, candidate.provider
                )
                response = await asyncio.wait_for(
                    provider.chat(
                        messages=[{"role": "user", "content": content}],
                        model=candidate.model,
                        max_tokens=candidate.max_tokens or default_max_tokens,
                        temperature=(
                            candidate.temperature
                            if candidate.temperature is not None
                            else default_temperature
                        ),
                    ),
                    timeout=timeout,
                )
            except Exception as exc:
                reason = (
                    f"timeout after {timeout:g} s"
                    if isinstance(exc, (asyncio.TimeoutError, TimeoutError))
                    else exc.__class__.__name__
                )
            else:
                text = (response.content or "").strip()
                if text:
                    return text
                if getattr(response, "finish_reason", None) != "error":
                    if not empty_is_failure:
                        # e.g. OCR of an image without text: a valid answer, not a failure.
                        return None
                    reason = "empty response"
                else:
                    reason = "provider error"
            if self._router is not None:
                # Cool the profile down so the next call starts with its fallback.
                self._router.mark_error(candidate.profile_name, error_type=reason)
            logger.warning(
                "vision {} failed model={} provider={} reason={}",
                task, candidate.model, candidate.provider, reason,
            )
            reasons.append(f"{candidate.model}: {reason}")
        if reasons:
            self._report_failure(task, chain[-1], "; ".join(reasons))
        return None

    @staticmethod
    def _image_block(image_path: Path) -> dict[str, object] | None:
        if not image_path.exists() or not image_path.is_file():
            return None
        mime, _ = mimetypes.guess_type(str(image_path))
        if not mime or not mime.startswith("image/"):
            return None
        b64 = base64.b64encode(image_path.read_bytes()).decode()
        return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}

    async def describe(self, image_path: Path, profile: ResolvedProfile) -> str | None:
        if profile.kind != "vision" or not profile.model:
            return None
        image = self._image_block(image_path)
        if image is None:
            return None
        text = await self._run(
            "image description",
            profile,
            [{"type": "text", "text": PROMPT}, image],
            timeout_s=(profile.timeout_ms or 12000) / 1000.0,
            default_max_tokens=160,
            default_temperature=0.1,
        )
        return " ".join(text.split()) if text else None

    async def ocr_image(self, image_path: Path, profile: ResolvedProfile) -> str | None:
        """Extract visible text from a local image using a routed vision/OCR model."""
        if profile.kind not in {"vision", "ocr"} or not profile.model:
            return None
        image = self._image_block(image_path)
        if image is None:
            return None
        return await self._run(
            "OCR",
            profile,
            [{"type": "text", "text": OCR_PROMPT}, image],
            timeout_s=(profile.timeout_ms or 12000) / 1000.0,
            default_max_tokens=1200,
            default_temperature=0.0,
            empty_is_failure=False,
        )

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
        text = await self._run(
            "video description",
            profile,
            [
                {"type": "video_url", "video_url": {"url": f"data:{mime};base64,{b64}"}},
                {"type": "text", "text": VIDEO_PROMPT},
            ],
            timeout_s=max((profile.timeout_ms or 0) / 1000.0, 60.0),
            default_max_tokens=320,
            default_temperature=0.1,
        )
        return " ".join(text.split()) if text else None
