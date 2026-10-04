"""Media artifact storage policy helpers."""

from __future__ import annotations

import time
from pathlib import Path

from loguru import logger
from yeoman_shared.raw_archive.paths import ProtectedPathError, assert_deletable_tree, is_protected
from yeoman_shared.utils.helpers import ensure_dir

_IMAGE_EXTENSIONS = frozenset(
    {
        ".avif",
        ".bmp",
        ".gif",
        ".heic",
        ".jpeg",
        ".jpg",
        ".png",
        ".tif",
        ".tiff",
        ".webp",
    }
)
_VIDEO_EXTENSIONS = frozenset(
    {
        ".3g2",
        ".3gp",
        ".avi",
        ".flv",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp4",
        ".mpeg",
        ".mpg",
        ".webm",
        ".wmv",
    }
)


class MediaStorage:
    """Validate and retain media files under configured roots."""

    def __init__(self, incoming_dir: Path, outgoing_dir: Path) -> None:
        self.incoming_dir = ensure_dir(incoming_dir.expanduser())
        self.outgoing_dir = ensure_dir(outgoing_dir.expanduser())

    def validate_incoming_path(self, path: str | Path | None) -> Path | None:
        """Return resolved path only when it is inside configured incoming root."""
        if not path:
            return None
        try:
            resolved = Path(path).expanduser().resolve()
            incoming_root = self.incoming_dir.expanduser().resolve()
            resolved.relative_to(incoming_root)
            return resolved
        except (OSError, RuntimeError, ValueError):
            return None

    def validate_outgoing_path(self, path: str | Path | None) -> Path | None:
        """Return resolved path only when it is inside configured outgoing root."""
        if not path:
            return None
        try:
            resolved = Path(path).expanduser().resolve()
            outgoing_root = self.outgoing_dir.expanduser().resolve()
            resolved.relative_to(outgoing_root)
            return resolved
        except (OSError, RuntimeError, ValueError):
            return None

    def cleanup_expired(
        self,
        channel: str,
        retention_days: int,
        *,
        image_retention_days: int | None = None,
        video_retention_days: int | None = None,
    ) -> int:
        """Delete incoming files by media type, with a common fallback window."""
        fallback_days = max(1, int(retention_days))
        image_days = max(
            1,
            int(image_retention_days if image_retention_days is not None else fallback_days),
        )
        video_days = max(
            1,
            int(video_retention_days if video_retention_days is not None else fallback_days),
        )
        now = time.time()
        thresholds = {
            "image": now - image_days * 24 * 60 * 60,
            "video": now - video_days * 24 * 60 * 60,
            "other": now - fallback_days * 24 * 60 * 60,
        }
        channel_dir = self._channel_dir(channel)
        if not channel_dir.exists():
            return 0
        try:
            assert_deletable_tree(channel_dir)
        except ProtectedPathError:
            logger.error("media cleanup refused: {} reaches the raw archive", channel_dir)
            return 0

        deleted = 0
        for path in sorted(channel_dir.rglob("*"), reverse=True):
            if path.is_file():
                if is_protected(path):
                    continue
                try:
                    suffix = path.suffix.lower()
                    kind = (
                        "image"
                        if suffix in _IMAGE_EXTENSIONS
                        else "video"
                        if suffix in _VIDEO_EXTENSIONS
                        else "other"
                    )
                    if path.stat().st_mtime < thresholds[kind]:
                        path.unlink()
                        deleted += 1
                except OSError:
                    continue
        return deleted

    def _channel_dir(self, channel: str) -> Path:
        """Resolve per-channel incoming folder while allowing channel-specific roots."""
        normalized = channel.strip().lower()
        if self.incoming_dir.name == normalized:
            return self.incoming_dir
        return self.incoming_dir / normalized
