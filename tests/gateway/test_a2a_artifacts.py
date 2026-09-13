from __future__ import annotations

import hashlib
import io
import os
import subprocess
import wave
from pathlib import Path

import pytest
from yeoman_gateway.a2a.artifacts import VoiceArtifactError, VoiceArtifactStore
from yeoman_gateway.app import bootstrap
from yeoman_gateway.media.router import ResolvedProfile
from yeoman_gateway.media.tts import (
    ElevenLabsTTSProvider,
    OpenAITTSProvider,
    TTSSynthesizer,
)


def _wav(*, frames: int = 800, rate: int = 8_000) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(b"\0\0" * frames)
    return output.getvalue()


def _ogg_opus(tmp_path: Path) -> bytes:
    source = tmp_path / "source.wav"
    target = tmp_path / "source.ogg"
    source.write_bytes(_wav())
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(source), "-c:a", "libopus", str(target)],
        check=True,
    )
    return target.read_bytes()


def _profile(*, provider: str = "openai_tts") -> ResolvedProfile:
    return ResolvedProfile(
        route_key="tts.speak",
        profile_name="tts_default",
        kind="tts",
        model="tts-1",
        provider=provider,
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
        managed_outgoing_root=tmp_path,
        tts=TTSSynthesizer(openai_api_key="fake"),
        model_router=router,
        max_bytes=10_000,
        ttl_seconds=60,
    )
    no_key = build(
        root=tmp_path / "no-key",
        managed_outgoing_root=tmp_path,
        tts=TTSSynthesizer(),
        model_router=router,
        max_bytes=10_000,
        ttl_seconds=60,
    )
    disabled = build(
        root=None,
        managed_outgoing_root=tmp_path,
        tts=TTSSynthesizer(openai_api_key="fake"),
        model_router=router,
        max_bytes=10_000,
        ttl_seconds=60,
    )

    assert enabled is not None and enabled.available
    assert no_key is None
    assert disabled is None
    assert router.calls == [("tts.speak", "whatsapp"), ("tts.speak", "whatsapp")]


def test_bootstrap_voice_store_requires_tools_and_managed_root_containment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    build = getattr(bootstrap, "build_a2a_voice_artifact_store")
    router = _Router(_profile())
    real_which = subprocess.run(["which", "ffprobe"], capture_output=True, text=True).stdout.strip()
    assert real_which

    outside = build(
        root=tmp_path / "outside",
        managed_outgoing_root=tmp_path / "managed",
        tts=TTSSynthesizer(openai_api_key="fake"),
        model_router=router,
        max_bytes=10_000,
        ttl_seconds=60,
    )
    monkeypatch.setattr(
        "yeoman_gateway.a2a.artifacts.shutil.which",
        lambda command: None if command == "ffprobe" else "/usr/bin/ffmpeg",
    )
    missing_probe = build(
        root=tmp_path / "managed" / "artifacts",
        managed_outgoing_root=tmp_path / "managed",
        tts=TTSSynthesizer(openai_api_key="fake"),
        model_router=router,
        max_bytes=10_000,
        ttl_seconds=60,
    )

    assert outside is None
    assert missing_probe is None


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
        managed_outgoing_root=tmp_path,
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
        managed_outgoing_root=tmp_path,
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
        managed_outgoing_root=tmp_path,
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("actual", "requested", "suffix", "mime_type"),
    [
        ("wav", "mp3", ".wav", "audio/wav"),
        ("ogg_opus", "wav", ".ogg", "audio/ogg; codecs=opus"),
    ],
)
async def test_provider_output_is_labeled_from_actual_container_and_codec(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    actual: str,
    requested: str,
    suffix: str,
    mime_type: str,
) -> None:
    fixture = _wav() if actual == "wav" else _ogg_opus(tmp_path)

    async def synthesize(self: object, **kwargs: object) -> tuple[bytes, None]:
        return fixture, None

    provider = "openai_tts" if actual == "wav" else "elevenlabs_tts"
    provider_class = OpenAITTSProvider if actual == "wav" else ElevenLabsTTSProvider
    monkeypatch.setattr(provider_class, "synthesize", synthesize)
    tts = (
        TTSSynthesizer(openai_api_key="fake")
        if actual == "wav"
        else TTSSynthesizer(
            elevenlabs_api_key="fake", elevenlabs_default_voice_id="voice-id"
        )
    )
    store = VoiceArtifactStore(
        tmp_path / "artifacts",
        tts=tts,
        profile=_profile(provider=provider),
        max_bytes=100_000,
        ttl_seconds=60,
        managed_outgoing_root=tmp_path,
    )

    result = await store.generate(
        "a2a-effect-" + "4" * 40,
        {"text": "Hello", "format": requested, "idempotency_key": "voice-4"},
    )

    assert Path(result["path"]).suffix == suffix
    assert result["mime_type"] == mime_type
    assert Path(result["path"]).read_bytes() == fixture


@pytest.mark.asyncio
async def test_generation_bounded_cleanup_removes_only_expired_owned_artifacts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def synthesize(self: object, **kwargs: object) -> tuple[bytes, None]:
        return _wav(), None

    monkeypatch.setattr(OpenAITTSProvider, "synthesize", synthesize)
    now = [1_000.0]
    store = VoiceArtifactStore(
        tmp_path / "artifacts",
        tts=TTSSynthesizer(openai_api_key="fake"),
        profile=_profile(),
        max_bytes=10_000,
        ttl_seconds=10,
        managed_outgoing_root=tmp_path,
        clock=lambda: now[0],
    )
    expired_id = "a2a-effect-" + "5" * 40
    await store.generate(
        expired_id,
        {"text": "Expired", "format": "wav", "idempotency_key": "voice-5"},
    )
    expired_path = Path(store._load(expired_id)["path"])  # type: ignore[index]
    unrelated = store.root / "do-not-delete.txt"
    unrelated.write_text("private")
    now[0] = 1_011.0

    await store.generate(
        "a2a-effect-" + "6" * 40,
        {"text": "Fresh", "format": "wav", "idempotency_key": "voice-6"},
    )

    assert not expired_path.exists()
    assert not store._sidecar(expired_id).exists()
    assert unrelated.read_text() == "private"
