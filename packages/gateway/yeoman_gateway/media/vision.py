"""Vision capability executor for image and video description."""

from __future__ import annotations

import asyncio
import base64
import mimetypes
from collections.abc import Callable, Sequence
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
    "Watch this short video to the very end so that someone in a group chat who cannot "
    "see it gets it - including the joke, if there is one. Still frames taken at regular "
    "intervals (timestamp in the corner) may follow the video so brief moments are not "
    "missed; use them together with the video.\n\n"
    "Answer in these labelled parts, each 1-3 sentences, in this order:\n"
    "Point: if the clip is meant to be funny or ironic, explain the punchline - the twist "
    "or contrast between the text, the images, the sound and how the people behave. If it "
    "is not a joke, say what the clip wants to show.\n"
    "Format: the kind of clip (meme/sketch, prank, news clip, tutorial, vlog, ad...) and "
    "its layout (split screen, text overlay, reaction cam).\n"
    "What happens: the events in order, with times. Watch closely for brief, unexpected "
    "moments (a collision, something falling, a sudden cut, a reveal) - they are often "
    "the point - and say exactly what happens in them.\n"
    "On-screen text: quote it exactly.\n"
    "Sound: tone of voice (calm, panicked, deadpan) and notable music or effects; spoken "
    "words are transcribed separately, so do not transcribe them.\n\n"
    "Describe only what is visible or audible, and do not identify people by name."
)

#: A whole clip is uploaded inline; slow providers need more than the image budget.
VIDEO_MIN_TIMEOUT_S = 90.0
#: GLM-class video models must reason before answering (it cannot be switched off), and
#: a 15 s clip already takes 100-400 reasoning tokens - a small cap returns no text at all.
VIDEO_MIN_MAX_TOKENS = 3000

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
        min_max_tokens: int = 0,
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
                        max_tokens=max(candidate.max_tokens or default_max_tokens, min_max_tokens),
                        reasoning=candidate.reasoning,
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
                finish = getattr(response, "finish_reason", None)
                if finish == "length":
                    # Reasoning models can spend the whole budget before answering.
                    reason = "output budget used up before an answer"
                elif finish != "error":
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
        *,
        frames: Sequence[Path] = (),
        context: str | None = None,
    ) -> str | None:
        """Send the complete local video to a video-capable routed model.

        ``frames`` are timestamped stills that make brief moments visible to models that
        sample the video sparsely; ``context`` is the post caption and speech transcript,
        which often carry the punchline.
        """
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
        content: list[dict[str, object]] = [
            {"type": "video_url", "video_url": {"url": f"data:{mime};base64,{b64}"}}
        ]
        for frame in frames:
            block = self._image_block(frame)
            if block is not None:
                content.append(block)
        prompt = VIDEO_PROMPT
        if context:
            prompt += (
                "\n\nContext shared with the video (use it to understand the joke; it may be "
                f"in another language):\n{context}"
            )
        content.append({"type": "text", "text": prompt})
        text = await self._run(
            "video description",
            profile,
            content,
            timeout_s=max((profile.timeout_ms or 0) / 1000.0, VIDEO_MIN_TIMEOUT_S),
            default_max_tokens=400,
            min_max_tokens=VIDEO_MIN_MAX_TOKENS,
            default_temperature=0.1,
        )
        if not text:
            return None
        lines = (" ".join(line.split()) for line in text.splitlines())
        return "\n".join(line for line in lines if line) or None
