from __future__ import annotations

import dataclasses
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


class _SlowFactory(_Factory):
    def create_chat_provider(self, model: str, provider_name: str | None = None):
        class _Provider:
            async def chat(self, **kwargs):
                import asyncio

                await asyncio.sleep(5)

        return _Provider()


class _EmptyFactory(_Factory):
    def __init__(self, finish_reason: str) -> None:
        super().__init__()
        self.finish_reason = finish_reason

    def create_chat_provider(self, model: str, provider_name: str | None = None):
        finish_reason = self.finish_reason

        class _Provider:
            async def chat(self, **kwargs):
                return SimpleNamespace(content="", finish_reason=finish_reason)

        return _Provider()


@pytest.mark.asyncio
async def test_failures_reach_the_listener_but_textless_ocr_does_not(tmp_path: Path) -> None:
    image = tmp_path / "synthetic.png"
    image.write_bytes(PNG)
    seen: list[tuple[str, str]] = []
    profile = _profile()
    profile = dataclasses.replace(profile, timeout_ms=1000)

    slow = VisionDescriber(_SlowFactory())
    slow.failure_listener = lambda task, _profile, reason: seen.append((task, reason))
    assert await slow.describe(image, profile) is None

    errored = VisionDescriber(_EmptyFactory("error"))
    errored.failure_listener = slow.failure_listener
    assert await errored.describe(image, profile) is None
    assert await errored.ocr_image(image, profile) is None

    textless = VisionDescriber(_EmptyFactory("stop"))
    textless.failure_listener = slow.failure_listener
    assert await textless.ocr_image(image, profile) is None

    assert seen == [
        ("image description", "z-ai/glm-5.3-flash: timeout after 1 s"),
        ("image description", "z-ai/glm-5.3-flash: provider error"),
        ("OCR", "z-ai/glm-5.3-flash: provider error"),
    ]


@pytest.mark.asyncio
async def test_a_failing_primary_falls_back_and_is_skipped_while_cooling_down(
    tmp_path: Path,
) -> None:
    from yeoman_gateway.media.router import ModelRouter
    from yeoman_shared.config.schema import ModelProfile, ModelRoutingConfig

    image = tmp_path / "synthetic.png"
    image.write_bytes(PNG)
    routing = ModelRoutingConfig(
        profiles={
            "vision_primary": ModelProfile(
                kind="vision", model="primary/model", provider="openrouter",
                fallback=["vision_backup"], cooldown_seconds=60,
            ),
            "vision_backup": ModelProfile(
                kind="vision", model="backup/model", provider="openrouter",
            ),
        },
        routes={"vision.describe_image": "vision_primary"},
    )
    router = ModelRouter(routing)
    used: list[str] = []

    class _Factory:
        def create_chat_provider(self, model: str, provider_name: str | None = None):
            class _Provider:
                async def chat(self, **kwargs):
                    used.append(model)
                    if model == "primary/model":
                        return SimpleNamespace(content="", finish_reason="stop")
                    return SimpleNamespace(content="a red circle", finish_reason="stop")

            return _Provider()

    seen: list[str] = []
    describer = VisionDescriber(_Factory(), router)
    describer.failure_listener = lambda task, _profile, reason: seen.append(reason)

    first = router.resolve("vision.describe_image")
    assert await describer.describe(image, first) == "a red circle"
    assert used == ["primary/model", "backup/model"]
    assert seen == []

    second = router.resolve("vision.describe_image")
    assert second.model == "backup/model"
    assert await describer.describe(image, second) == "a red circle"
    assert used[-1] == "backup/model" and len(used) == 3


@pytest.mark.asyncio
async def test_vision_alert_goes_to_telegram_owners_once_per_cooldown() -> None:
    import asyncio

    from yeoman_gateway.app.bootstrap import _build_vision_failure_alert

    sent: list[tuple[str, str, str]] = []
    now = [0.0]

    async def notify(channel: str, chat_id: str, text: str) -> None:
        sent.append((channel, chat_id, text))

    alert = _build_vision_failure_alert(
        lambda channel: ["111", " "] if channel == "telegram" else ["whatsapp-owner"],
        notify,
        cooldown_seconds=300,
        clock=lambda: now[0],
    )
    profile = _profile()
    alert("image description", profile, "timeout after 12 s")
    alert("image description", profile, "timeout after 12 s")
    now[0] = 301
    alert("image description", profile, "provider error")
    await asyncio.sleep(0)

    assert [(channel, chat) for channel, chat, _ in sent] == [("telegram", "111")] * 2
    assert "image description failed: timeout after 12 s" in sent[0][2]
    assert "model=z-ai/glm-5.3-flash" in sent[0][2]
