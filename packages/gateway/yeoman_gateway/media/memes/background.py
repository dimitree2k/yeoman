"""Text-free meme backgrounds from an image model routed through OpenRouter."""

from __future__ import annotations

import base64
from io import BytesIO
from typing import Any

import httpx
from loguru import logger
from PIL import Image

from yeoman_gateway.media.router import ResolvedProfile
from yeoman_gateway.providers.factory import ProviderCredentials

#: Image models garble lettering, so captions are always drawn by Pillow afterwards.
_NO_TEXT_SUFFIX = (
    " Leave clear empty space at the top and bottom for captions. "
    "Absolutely no text, letters, numbers, logos, signs or watermarks anywhere in the image."
)
_MAX_PROMPT_CHARS = 600
_MAX_IMAGE_BYTES = 12 * 1024 * 1024


class BackgroundError(RuntimeError):
    """Image generation failed; the message is safe to show the model."""


def _decode_data_url(url: str) -> bytes:
    header, _, payload = url.partition(",")
    if not header.startswith("data:image/") or ";base64" not in header:
        raise BackgroundError("image model returned an unsupported data URL")
    return base64.b64decode(payload, validate=False)


def _first_image_url(body: dict[str, Any]) -> str:
    choices = body.get("choices") or []
    message = choices[0].get("message") if choices and isinstance(choices[0], dict) else None
    for image in (message or {}).get("images") or []:
        url = ((image or {}).get("image_url") or {}).get("url")
        if isinstance(url, str) and url:
            return url
    raise BackgroundError("image model returned no image")


class BackgroundGenerator:
    """Generate one background image for a meme via OpenAI-compatible chat completions."""

    def __init__(
        self,
        credentials: ProviderCredentials,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._credentials = credentials
        self._transport = transport

    async def generate(self, prompt: str, profile: ResolvedProfile) -> Image.Image:
        if profile.kind != "image" or not profile.model:
            raise BackgroundError(f"route {profile.route_key} is not an image profile")
        if not self._credentials.api_key:
            raise BackgroundError("no API key configured for the image provider")
        scene = " ".join(str(prompt or "").split())[:_MAX_PROMPT_CHARS]
        if not scene:
            raise BackgroundError("background prompt is empty")
        base = (self._credentials.api_base or "https://openrouter.ai/api/v1").rstrip("/")
        headers = {
            "Authorization": f"Bearer {self._credentials.api_key}",
            "Content-Type": "application/json",
            **(self._credentials.extra_headers or {}),
        }
        payload = {
            "model": profile.model,
            "messages": [{"role": "user", "content": scene + _NO_TEXT_SUFFIX}],
            "modalities": ["image"],
            "image_config": {"aspect_ratio": "1:1"},
        }
        timeout = max(5.0, (profile.timeout_ms or 90_000) / 1000.0)
        async with httpx.AsyncClient(transport=self._transport, timeout=timeout) as client:
            try:
                response = await client.post(
                    f"{base}/chat/completions", headers=headers, json=payload
                )
            except httpx.HTTPError as exc:
                raise BackgroundError(f"image request failed ({exc.__class__.__name__})") from exc
            if response.status_code >= 400:
                logger.warning(
                    "meme background http {} model={}: {}",
                    response.status_code,
                    profile.model,
                    response.text[:300],
                )
                raise BackgroundError(f"image model returned HTTP {response.status_code}")
            url = _first_image_url(response.json())
            if url.startswith("data:"):
                raw = _decode_data_url(url)
            elif url.startswith("https://"):
                try:
                    fetched = await client.get(url)
                    fetched.raise_for_status()
                except httpx.HTTPError as exc:
                    raise BackgroundError("generated image could not be downloaded") from exc
                raw = fetched.content
            else:
                raise BackgroundError("image model returned an unsupported image URL")
        if len(raw) > _MAX_IMAGE_BYTES:
            raise BackgroundError("generated image is too large")
        try:
            with Image.open(BytesIO(raw)) as image:
                image.load()
                return image.convert("RGB")
        except (OSError, ValueError) as exc:
            raise BackgroundError("generated image could not be decoded") from exc
