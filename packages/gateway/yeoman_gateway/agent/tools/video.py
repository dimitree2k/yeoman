"""video_understand: watch a video link (Instagram reel, TikTok, Shorts, ...)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from loguru import logger

from yeoman_gateway.agent.tools.base import Tool

if TYPE_CHECKING:
    from yeoman_gateway.media.video_understanding import VideoUnderstanding


class VideoUnderstandTool(Tool):
    """Download a short video from a link and return caption, description and transcript."""

    external_content = True

    name = "video_understand"
    description = (
        "Watch a video from a link and return what it shows, what is said in it and the "
        "post's caption. Use this for Instagram reels/posts, TikTok, YouTube Shorts, X/Twitter, "
        "Facebook, Reddit and other video links whenever you need to know what a shared video "
        "is about; web_fetch and browse cannot see video content. For long YouTube videos "
        "use youtube_transcript instead. Clips up to a few minutes; takes 10-60 s."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Full link to the video or post"},
        },
        "required": ["url"],
    }

    def __init__(self, service: "VideoUnderstanding | None" = None) -> None:
        self._service = service

    async def execute(self, url: str, **kwargs: Any) -> str:
        if self._service is None:
            return json.dumps({"error": "Video understanding is not configured on this host."})
        from yeoman_gateway.media.video_understanding import VideoUnderstandingError

        try:
            insight = await self._service.understand_url(url)
        except VideoUnderstandingError as exc:
            return json.dumps({"error": str(exc), "url": url})
        except Exception as exc:  # noqa: BLE001 - downloader/provider failures must not break the turn
            logger.warning("video_understand failed {}", type(exc).__name__)
            return json.dumps({"error": "Could not process the video.", "url": url})
        if insight.empty:
            return json.dumps({"error": "Nothing could be extracted from this video.", "url": url})
        result = insight.as_dict()
        if insight.description is None and insight.transcript is None:
            result["note"] = (
                "Only the post's metadata was available (clip too long or not downloadable)."
            )
        return json.dumps(result, ensure_ascii=False)
