from __future__ import annotations

import hashlib
import io
import os
import wave
from pathlib import Path

import pytest
from yeoman_gateway.a2a.artifacts import VoiceArtifactError, VoiceArtifactStore
from yeoman_gateway.app import bootstrap
from yeoman_gateway.media.router import ResolvedProfile
from yeoman_gateway.media.tts import OpenAITTSProvider, TTSSynthesizer


def _wav(*, frames: int = 800, rate: int = 8_000) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(b"\0\0" * frames)
    return output.getvalue()


def _profile() -> ResolvedProfile:
    return ResolvedProfile(
        route_key="tts.speak",
        profile_name="tts_default",
        kind="tts",
        model="tts-1",
        provider="openai_tts",
        max_tokens=None,
        temperature=None,
        timeout_ms=1_000,
    )


class _Router:
    def __init__(self, profile: ResolvedProfile) -> None:
        self.profile = profile
        self.calls: list[tuple[str, str | None]] = []

    def resolve(self, route: str, channel: str | None = None) -> ResolvedProfile:
        self.calls.append((route, channel))
        return self.profile


def test_bootstrap_voice_store_requires_configured_key_route_and_directory(
    tmp_path: Path,
) -> None:
    build = getattr(bootstrap, "build_a2a_voice_artifact_store")
    router = _Router(_profile())

    enabled = build(
        root=tmp_path / "artifacts",
        tts=TTSSynthesizer(openai_api_key="fake"),
        model_router=router,
        max_bytes=10_000,
        ttl_seconds=60,
    )
    no_key = build(
        root=tmp_path / "no-key",
        tts=TTSSynthesizer(),
        model_router=router,
        max_bytes=10_000,
        ttl_seconds=60,
    )
    disabled = build(
        root=None,
        tts=TTSSynthesizer(openai_api_key="fake"),
        model_router=router,
        max_bytes=10_000,
        ttl_seconds=60,
    )

    assert enabled is not None and enabled.available
    assert no_key is None
    assert disabled is None
    assert router.calls == [("tts.speak", "whatsapp"), ("tts.speak", "whatsapp")]


@pytest.mark.asyncio
async def test_real_tts_wav_is_private_deterministic_and_reused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fixture = _wav()
    calls = 0

    async def synthesize(self: object, **kwargs: object) -> tuple[bytes, None]:
        nonlocal calls
        calls += 1
        assert kwargs == {
            "text": "Hello voice",
            "model": "tts-1",
            "voice": "alloy",
            "format": "wav",
        }
        return fixture, None

    monkeypatch.setattr(OpenAITTSProvider, "synthesize", synthesize)
    store = VoiceArtifactStore(
        tmp_path / "artifacts",
        tts=TTSSynthesizer(openai_api_key="fake"),
        profile=_profile(),
        max_bytes=10_000,
        ttl_seconds=60,
        clock=lambda: 1_000.0,
    )
    request = {
        "text": "Hello voice",
        "voice": "alloy",
        "format": "wav",
        "idempotency_key": "voice-1",
    }

    first = await store.generate("a2a-effect-" + "1" * 40, request)
    second = await store.generate("a2a-effect-" + "1" * 40, request)

    path = Path(first["path"])
    assert first == second
    assert calls == 1
    assert path.read_bytes() == fixture
    assert path.name == "a2a-effect-" + "1" * 40 + ".wav"
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert first == {
        "path": str(path),
        "mime_type": "audio/wav",
        "duration_ms": 100,
        "sha256": hashlib.sha256(fixture).hexdigest(),
        "size_bytes": len(fixture),
        "expires_at": 1_060.0,
        "filename": path.name,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("audio", "max_bytes", "code"),
    [
        (b"not audio", 10_000, "INVALID_AUDIO"),
        (_wav(), 10, "ARTIFACT_TOO_LARGE"),
    ],
)
async def test_invalid_or_oversized_audio_fails_closed_without_artifact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    audio: bytes,
    max_bytes: int,
    code: str,
) -> None:
    async def synthesize(self: object, **kwargs: object) -> tuple[bytes, None]:
        return audio, None

    monkeypatch.setattr(OpenAITTSProvider, "synthesize", synthesize)
    store = VoiceArtifactStore(
        tmp_path / "artifacts",
        tts=TTSSynthesizer(openai_api_key="fake"),
        profile=_profile(),
        max_bytes=max_bytes,
        ttl_seconds=60,
    )

    with pytest.raises(VoiceArtifactError, match=code):
        await store.generate(
            "a2a-effect-" + "2" * 40,
            {"text": "Hello", "format": "wav", "idempotency_key": "voice-2"},
        )

    assert list((tmp_path / "artifacts").glob("*.wav")) == []


@pytest.mark.asyncio
async def test_same_effect_with_different_request_is_a_conflict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def synthesize(self: object, **kwargs: object) -> tuple[bytes, None]:
        return _wav(), None

    monkeypatch.setattr(OpenAITTSProvider, "synthesize", synthesize)
    store = VoiceArtifactStore(
        tmp_path / "artifacts",
        tts=TTSSynthesizer(openai_api_key="fake"),
        profile=_profile(),
        max_bytes=10_000,
        ttl_seconds=60,
    )
    effect_id = "a2a-effect-" + "3" * 40
    await store.generate(
        effect_id,
        {"text": "First", "format": "wav", "idempotency_key": "voice-3"},
    )

    with pytest.raises(VoiceArtifactError, match="ARTIFACT_CONFLICT"):
        await store.generate(
            effect_id,
            {"text": "Changed", "format": "wav", "idempotency_key": "voice-3"},
        )
