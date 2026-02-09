"""Vision capability executor for image description."""

from __future__ import annotations

import asyncio
import base64
import mimetypes
from pathlib import Path

from nanobot.media.router import ResolvedProfile
from nanobot.providers.factory import ProviderFactory

PROMPT = (
    "Describe this image in 1-2 concise sentences. "
    "Be factual, include key objects/action, and mention visible text only if readable."
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
