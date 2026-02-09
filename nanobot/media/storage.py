"""Media artifact storage policy helpers."""

from __future__ import annotations

import time
from pathlib import Path

from nanobot.utils.helpers import ensure_dir


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

    def cleanup_expired(self, channel: str, retention_days: int) -> int:
        """Delete incoming files older than retention window for one channel."""
        days = max(1, int(retention_days))
        threshold = time.time() - (days * 24 * 60 * 60)
        channel_dir = self._channel_dir(channel)
        if not channel_dir.exists():
            return 0

        deleted = 0
        for path in sorted(channel_dir.rglob("*"), reverse=True):
            if path.is_file():
                try:
                    if path.stat().st_mtime < threshold:
                        path.unlink()
                        deleted += 1
                except OSError:
                    continue
            elif path.is_dir():
                try:
                    path.rmdir()
                except OSError:
                    continue
        return deleted

    def _channel_dir(self, channel: str) -> Path:
        """Resolve per-channel incoming folder while allowing channel-specific roots."""
        normalized = channel.strip().lower()
        if self.incoming_dir.name == normalized:
            return self.incoming_dir
        return self.incoming_dir / normalized
