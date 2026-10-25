from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from yeoman_gateway.media.router import ResolvedProfile
from yeoman_gateway.media.vision import VisionDescriber

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


class _Factory:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def create_chat_provider(self, model: str, provider_name: str | None = None):
        self.calls.append((model, provider_name))

        class _Provider:
            async def chat(self, **kwargs):
                return SimpleNamespace(content="a red circle")

        return _Provider()


def _profile(kind: str = "vision") -> ResolvedProfile:
    return ResolvedProfile(
        route_key="vision.describe_image",
        profile_name="vision_test",
        kind=kind,
        model="z-ai/glm-5.3-flash",
        provider="openrouter",
        max_tokens=100,
        temperature=0.1,
        timeout_ms=5000,
    )


@pytest.mark.asyncio
async def test_image_description_and_ocr_use_the_profiles_provider(tmp_path: Path) -> None:
    image = tmp_path / "synthetic.png"
    image.write_bytes(PNG)
    factory = _Factory()
    describer = VisionDescriber(factory)

    assert await describer.describe(image, _profile()) == "a red circle"
    assert await describer.ocr_image(image, _profile("ocr")) == "a red circle"
    assert factory.calls == [("z-ai/glm-5.3-flash", "openrouter")] * 2
