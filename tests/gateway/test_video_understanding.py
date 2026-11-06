"""Channel-agnostic video understanding: files, links, the tool and channel wiring."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from yeoman_gateway.agent.tools.video import VideoUnderstandTool
from yeoman_gateway.media import video_understanding as vu
from yeoman_gateway.media.asr import ASRResult
from yeoman_gateway.media.router import ResolvedProfile
from yeoman_gateway.media.video_understanding import (
    VideoInsight,
    VideoUnderstanding,
    VideoUnderstandingError,
)


class _Router:
    def resolve(self, task: str, channel: str | None = None) -> Any:
        return ResolvedProfile(
            route_key=task, profile_name=task, kind="vision", model=f"{task}-model",
            provider=None, max_tokens=None, temperature=None, timeout_ms=1000,
        )


class _Describer:
    def __init__(self, text: str | None = "A cat opens a door.", delay: float = 0.05) -> None:
        self.text = text
        self.delay = delay
        self.calls = 0

    async def describe_video(self, path: Path, profile: Any) -> str | None:
        self.calls += 1
        await asyncio.sleep(self.delay)
        return self.text


class _Transcriber:
    def __init__(self, text: str = "hello  there", delay: float = 0.05) -> None:
        self.text = text
        self.delay = delay
        self.profiles: list[Any] = []

    async def transcribe(self, path: Path, profile: Any) -> ASRResult:
        self.profiles.append(profile)
        await asyncio.sleep(self.delay)
        return ASRResult(text=self.text, model="whisper-large-v3")


@pytest.fixture
def clip(tmp_path: Path) -> Path:
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"not really a video")
    return path


def _patch_media_tools(monkeypatch: pytest.MonkeyPatch, *, has_audio: bool = True) -> None:
    async def probe(path: Path) -> tuple[float | None, bool]:
        return 12.04, has_audio

    async def extract(source: Path, target: Path) -> bool:
        target.write_bytes(b"mp3")
        return True

    monkeypatch.setattr(vu, "probe_video", probe)
    monkeypatch.setattr(vu, "extract_audio", extract)


def _service(describer: Any = None, transcriber: Any = None, **kwargs: Any) -> VideoUnderstanding:
    return VideoUnderstanding(
        describer=describer if describer is not None else _Describer(),
        router=_Router(),
        transcriber=transcriber if transcriber is not None else _Transcriber(),
        **kwargs,
    )


def test_file_is_described_and_transcribed_concurrently(
    monkeypatch: pytest.MonkeyPatch, clip: Path
) -> None:
    _patch_media_tools(monkeypatch)
    describer, transcriber = _Describer(delay=0.3), _Transcriber(delay=0.3)
    service = _service(describer, transcriber)

    async def run() -> tuple[VideoInsight, float]:
        loop = asyncio.get_running_loop()
        start = loop.time()
        insight = await service.understand_file(clip, channel="telegram")
        return insight, loop.time() - start

    insight, elapsed = asyncio.run(run())

    assert insight.description == "A cat opens a door."
    assert insight.transcript == "hello there"
    assert insight.description_model == "vision.describe_video-model"
    assert insight.transcript_model == "whisper-large-v3"
    assert insight.duration_s == 12.0
    assert elapsed < 0.55  # both calls overlapped instead of 0.6 s in sequence
    # Speech-to-text gets room for a whole clip, whatever the voice-note budget is.
    assert transcriber.profiles[0].timeout_ms >= 120_000
    assert insight.enrichment_lines() == [
        "[video_description] A cat opens a door.",
        "[video_transcript] hello there",
    ]


def test_silent_clip_is_not_sent_to_speech_to_text(
    monkeypatch: pytest.MonkeyPatch, clip: Path
) -> None:
    _patch_media_tools(monkeypatch, has_audio=False)
    transcriber = _Transcriber()
    insight = asyncio.run(_service(transcriber=transcriber).understand_file(clip))
    assert insight.description and insight.transcript is None
    assert transcriber.profiles == []


def test_oversized_clip_keeps_transcript_without_inline_upload(
    monkeypatch: pytest.MonkeyPatch, clip: Path
) -> None:
    _patch_media_tools(monkeypatch)
    describer = _Describer()
    insight = asyncio.run(
        _service(describer, max_describe_bytes=4).understand_file(clip)
    )
    assert describer.calls == 0
    assert insight.description is None and insight.transcript == "hello there"


def test_same_file_is_understood_once(monkeypatch: pytest.MonkeyPatch, clip: Path) -> None:
    _patch_media_tools(monkeypatch)
    describer = _Describer()
    service = _service(describer)

    async def twice() -> None:
        await service.understand_file(clip)
        await service.understand_file(clip)

    asyncio.run(twice())
    assert describer.calls == 1


def test_link_returns_caption_and_watched_content(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_media_tools(monkeypatch)
    downloads: list[str] = []

    def download(self: VideoUnderstanding, url: str, target: Path) -> tuple[dict, Path]:
        downloads.append(url)
        path = target / "video.mp4"
        path.write_bytes(b"clip")
        info = {
            "webpage_url": url, "extractor_key": "Instagram", "title": "Video by someone",
            "description": "Best   pasta\nrecipe ever", "uploader": "someone", "duration": 30,
        }
        return info, path

    monkeypatch.setattr(VideoUnderstanding, "_download", download)
    service = _service()
    url = "https://www.instagram.com/reel/ABC123/"

    async def run() -> tuple[VideoInsight, VideoInsight]:
        first, second = await asyncio.gather(service.understand_url(url), service.understand_url(url))
        return first, second

    first, second = asyncio.run(run())

    assert downloads == [url]  # concurrent requests for one link share a download
    assert first == second
    assert first.site == "Instagram" and first.caption == "Best pasta recipe ever"
    assert first.description == "A cat opens a door." and first.transcript == "hello there"


def test_long_link_returns_metadata_only(monkeypatch: pytest.MonkeyPatch) -> None:
    def download(self: VideoUnderstanding, url: str, target: Path) -> tuple[dict, None]:
        return {"title": "Two hour podcast", "duration": 7200}, None

    monkeypatch.setattr(VideoUnderstanding, "_download", download)
    insight = asyncio.run(_service().understand_url("https://example.com/v/1"))
    assert insight.title == "Two hour podcast" and insight.description is None
    assert insight.duration_s == 7200


def test_download_error_becomes_user_facing_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from yt_dlp.utils import DownloadError

    class FakeYDL:
        def __init__(self, options: dict) -> None:
            self.options = options

        def __enter__(self) -> "FakeYDL":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def extract_info(self, url: str, download: bool) -> dict:
            raise DownloadError(
                "ERROR: [Instagram] X: Requested content is not available, rate-limit reached "
                "or login required. Use --cookies"
            )

    import yt_dlp

    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    with pytest.raises(VideoUnderstandingError) as excinfo:
        _service()._download("https://www.instagram.com/reel/X/", tmp_path)
    assert "login required" in str(excinfo.value)
    assert not str(excinfo.value).startswith("Could not fetch the video: ERROR")


def test_cookie_file_is_passed_only_when_configured(tmp_path: Path) -> None:
    assert "cookiefile" not in _service()._ydl_options(tmp_path)
    options = _service(cookies_file="/secure/cookies.txt")._ydl_options(tmp_path)
    assert options["cookiefile"] == "/secure/cookies.txt"
    assert "height<=480" in options["format"]


def test_non_web_link_is_rejected() -> None:
    with pytest.raises(VideoUnderstandingError):
        asyncio.run(_service().understand_url("file:///etc/passwd"))


# -- tool ------------------------------------------------------------------------------


class _FakeService:
    def __init__(self, result: VideoInsight | Exception) -> None:
        self.result = result

    async def understand_url(self, url: str) -> VideoInsight:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_tool_returns_structured_result() -> None:
    tool = VideoUnderstandTool(
        _FakeService(VideoInsight(caption="Ciao", description="Pasta is cooked.", transcript="ok"))
    )
    data = json.loads(asyncio.run(tool.execute(url="https://www.instagram.com/reel/A/")))
    assert data == {"caption": "Ciao", "description": "Pasta is cooked.", "transcript": "ok"}
    assert tool.external_content is True


def test_tool_reports_download_failure_honestly() -> None:
    tool = VideoUnderstandTool(_FakeService(VideoUnderstandingError("Could not fetch: login required")))
    data = json.loads(asyncio.run(tool.execute(url="https://www.instagram.com/reel/A/")))
    assert data["error"] == "Could not fetch: login required"


def test_tool_notes_metadata_only_result() -> None:
    tool = VideoUnderstandTool(_FakeService(VideoInsight(title="Long talk", duration_s=5000)))
    data = json.loads(asyncio.run(tool.execute(url="https://example.com/v")))
    assert data["title"] == "Long talk" and "note" in data


def test_tool_without_service_says_so() -> None:
    data = json.loads(asyncio.run(VideoUnderstandTool(None).execute(url="https://x.test/v")))
    assert "not configured" in data["error"]


def test_video_understand_is_an_owner_default_tool() -> None:
    from yeoman_gateway.policy.schema import DEFAULT_OWNER_TOOLS

    assert "video_understand" in DEFAULT_OWNER_TOOLS


def test_web_fetch_sends_video_posts_to_video_tool() -> None:
    from yeoman_gateway.agent.tools.web import _VIDEO_POST_URL

    assert _VIDEO_POST_URL.search("https://www.instagram.com/reel/C5Zl6o5uJbE/")
    assert _VIDEO_POST_URL.search("https://instagram.com/p/C5Zl6o5uJbE/?igsh=x")
    assert _VIDEO_POST_URL.search("https://vm.tiktok.com/ZMabc/")
    assert not _VIDEO_POST_URL.search("https://www.instagram.com/someprofile/")
    assert not _VIDEO_POST_URL.search("https://example.com/instagram.com/reel/x")


def test_video_tool_config_defaults() -> None:
    from yeoman_shared.config.schema import Config

    video = Config().tools.video
    assert video.enabled and video.cookies_file == ""
    assert video.max_url_duration_seconds == 180


# -- telegram --------------------------------------------------------------------------


def test_telegram_video_gets_description_and_transcript(tmp_path: Path) -> None:
    from yeoman_gateway.bus.queue import MessageBus
    from yeoman_gateway.channels.telegram import TelegramChannel
    from yeoman_shared.config.schema import TelegramConfig

    channel = TelegramChannel(TelegramConfig(), MessageBus())
    assert channel._get_extension("video", None) == ".mp4"

    class Service:
        async def understand_file(self, path: Path, *, channel: str) -> VideoInsight:
            assert channel == "telegram"
            return VideoInsight(description="Fireworks over a bay.", transcript="wow")

    channel.set_video_understanding(Service())
    lines = asyncio.run(channel._understand_video(tmp_path / "v.mp4"))
    assert lines == ["[video_description] Fireworks over a bay.", "[video_transcript] wow"]

    channel.set_video_understanding(SimpleNamespace(understand_file=_raise))
    assert asyncio.run(channel._understand_video(tmp_path / "v.mp4")) == [
        f"[video: {tmp_path / 'v.mp4'}]"
    ]


async def _raise(*args: object, **kwargs: object) -> VideoInsight:
    raise RuntimeError("provider down")


# -- vision executor -------------------------------------------------------------------


def test_video_call_leaves_room_for_mandatory_reasoning(tmp_path: Path) -> None:
    from yeoman_gateway.media.vision import VIDEO_MIN_MAX_TOKENS, VisionDescriber

    calls: list[dict[str, Any]] = []
    replies = iter([
        SimpleNamespace(content="", finish_reason="length"),
        SimpleNamespace(content="A dog runs.", finish_reason="stop"),
    ])

    class Provider:
        async def chat(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            return next(replies)

    factory = SimpleNamespace(create_chat_provider=lambda model, provider: Provider())
    describer = VisionDescriber(factory)  # type: ignore[arg-type]
    clip = tmp_path / "c.mp4"
    clip.write_bytes(b"x")
    profile = ResolvedProfile(
        route_key="vision.describe_video", profile_name="vision_video", kind="vision",
        model="z-ai/glm-5.3-flash", provider="openrouter", max_tokens=400, temperature=0.1,
        timeout_ms=12000, reasoning={"effort": "low"},
    )
    failures: list[str] = []
    describer.failure_listener = lambda task, prof, reason: failures.append(reason)

    assert asyncio.run(describer.describe_video(clip, profile)) is None
    assert failures == ["z-ai/glm-5.3-flash: output budget used up before an answer"]
    assert calls[0]["max_tokens"] == VIDEO_MIN_MAX_TOKENS
    assert calls[0]["reasoning"] == {"effort": "low"}
    assert asyncio.run(describer.describe_video(clip, profile)) == "A dog runs."


def test_long_description_is_cut_at_a_sentence_end(
    monkeypatch: pytest.MonkeyPatch, clip: Path
) -> None:
    _patch_media_tools(monkeypatch)
    long_text = " ".join(f"Sentence number {i} describes the scene." for i in range(200))
    insight = asyncio.run(_service(_Describer(text=long_text)).understand_file(clip))
    assert insight.description is not None
    assert len(insight.description) <= vu._MAX_DESCRIPTION_CHARS
    assert insight.description.endswith("scene.")
    assert vu._clip_sentences("short  text", 50) == "short text"
