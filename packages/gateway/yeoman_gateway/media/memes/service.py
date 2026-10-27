"""Render memes to files the WhatsApp channel may send."""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from loguru import logger
from PIL import Image

from yeoman_gateway.media.memes.background import BackgroundError, BackgroundGenerator
from yeoman_gateway.media.memes.render import MAX_OUTPUT_SIDE, TOP_BOTTOM, render_meme
from yeoman_gateway.media.memes.templates import MemeTemplate, load_templates

if TYPE_CHECKING:
    from yeoman_gateway.media.router import ModelRouter

BACKGROUND_ROUTE = "meme.background"
MAX_CAPTIONS = 4
MAX_CAPTION_CHARS = 140
#: Inbound photos larger than this are refused instead of decoded.
MAX_SOURCE_PIXELS = 40_000_000
_KEEP_RENDERED_SECONDS = 7 * 24 * 60 * 60


class MemeError(RuntimeError):
    """A meme could not be made; the message is safe to show the model."""


class DailyCap:
    """Persistent per-day counter shared by all chats (one JSON file, one process)."""

    def __init__(self, path: Path, limit: int, today: Callable[[], date] = date.today) -> None:
        self._path = path
        self._limit = max(0, int(limit))
        self._today = today
        self._lock = asyncio.Lock()

    def _read(self) -> dict[str, int]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except OSError, ValueError:
            return {}
        return {str(k): int(v) for k, v in raw.items()} if isinstance(raw, dict) else {}

    def _write(self, counts: dict[str, int]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(counts), encoding="utf-8")
        os.replace(tmp, self._path)

    async def claim(self) -> bool:
        async with self._lock:
            day = self._today().isoformat()
            used = self._read().get(day, 0)
            if used >= self._limit:
                return False
            # Only today's entry is kept, so the file never grows.
            self._write({day: used + 1})
            return True

    async def refund(self) -> None:
        async with self._lock:
            day = self._today().isoformat()
            used = self._read().get(day, 0)
            if used > 0:
                self._write({day: used - 1})


class MemeService:
    """Make template, photo and generated-background memes as JPEG files."""

    def __init__(
        self,
        *,
        outgoing_dir: Path,
        router: "ModelRouter | None" = None,
        background: BackgroundGenerator | None = None,
        background_cap: DailyCap | None = None,
        templates: dict[str, MemeTemplate] | None = None,
    ) -> None:
        self._out_dir = Path(outgoing_dir).expanduser() / "memes"
        self._router = router
        self._background = background
        self._cap = background_cap
        self.templates = templates if templates is not None else load_templates()

    @property
    def backgrounds_available(self) -> bool:
        return self._background is not None and self._router is not None and self._cap is not None

    def template_catalog(self) -> str:
        """Compact `id(boxes)` list for the tool description."""
        return ", ".join(f"{t.id}({len(t.boxes)})" for t in self.templates.values())

    @staticmethod
    def clean_captions(captions: list[str] | None) -> list[str]:
        cleaned = [" ".join(str(c or "").split())[:MAX_CAPTION_CHARS] for c in captions or []]
        if not any(cleaned):
            raise MemeError("at least one caption is required")
        return cleaned[:MAX_CAPTIONS]

    async def from_template(self, template_id: str, captions: list[str]) -> Path:
        template = self.templates.get(str(template_id or "").strip().lower())
        if template is None:
            raise MemeError(f"unknown template '{template_id}'; pick one from the list")
        lines = self.clean_captions(captions)
        with Image.open(template.image_path) as base:
            return await self._save(base, lines, template.boxes)

    async def from_photo(self, path: Path, captions: list[str]) -> Path:
        lines = self.clean_captions(captions)
        try:
            with Image.open(path) as base:
                if base.width * base.height > MAX_SOURCE_PIXELS:
                    raise MemeError("that photo is too large to meme")
                # JPEG decodes straight at a reduced scale; the final resize stays exact.
                base.draft("RGB", (MAX_OUTPUT_SIDE, MAX_OUTPUT_SIDE))
                base.load()
                return await self._save(base, lines, TOP_BOTTOM)
        except (OSError, ValueError, Image.DecompressionBombError) as exc:
            raise MemeError("that photo could not be opened") from exc

    async def from_background(self, prompt: str, captions: list[str], channel: str) -> Path:
        lines = self.clean_captions(captions)
        if not self.backgrounds_available:
            raise MemeError("generated backgrounds are not configured")
        assert self._router is not None and self._cap is not None and self._background is not None
        try:
            profile = self._router.resolve(BACKGROUND_ROUTE, channel=channel)
        except KeyError as exc:
            raise MemeError(f"model route {BACKGROUND_ROUTE} is not configured") from exc
        if not await self._cap.claim():
            raise MemeError("the daily budget for generated backgrounds is used up")
        try:
            base = await self._background.generate(prompt, profile)
        except BackgroundError as exc:
            await self._cap.refund()
            self._router.mark_error(profile.profile_name, error_type="image")
            raise MemeError(f"background generation failed: {exc}") from exc
        return await self._save(base, lines, TOP_BOTTOM)

    async def _save(self, base: Image.Image, lines: list[str], boxes: tuple) -> Path:
        def work() -> Path:
            image = render_meme(base, lines, boxes)
            self._out_dir.mkdir(parents=True, exist_ok=True)
            self._prune()
            path = self._out_dir / f"meme-{uuid.uuid4().hex}.jpg"
            image.save(path, "JPEG", quality=88, optimize=True)
            return path

        # Rendering takes tens to hundreds of milliseconds on a Pi; keep it off the event loop.
        path = await asyncio.to_thread(work)
        logger.info("meme rendered path={} captions={}", path.name, len(lines))
        return path

    def _prune(self) -> None:
        """Drop rendered memes older than a week; the outbox has long delivered them."""
        cutoff = time.time() - _KEEP_RENDERED_SECONDS
        for old in self._out_dir.glob("meme-*.jpg"):
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink()
            except OSError:
                continue
