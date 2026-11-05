"""Owner intent per managed service: running, paused (optionally until a time) or stopped.

The Overseer, the CLI and the gateway all read the same file, so a service the owner
paused is never restarted behind their back. Times are wall-clock Unix seconds so a
pause survives reboots. Writes take an exclusive ``fcntl`` lock and replace the file
atomically.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from yeoman_shared.utils.helpers import get_operational_store_path

Desired = Literal["running", "paused", "stopped"]

# Short names the CLI accepts. Telegram is a channel inside the gateway, not its own unit.
SERVICE_ALIASES: dict[str, str] = {
    "bridge": "yeoman-bridge",
    "whatsapp": "yeoman-bridge",
    "gateway": "yeoman-gateway",
    "telegram": "yeoman-gateway/telegram",
    "overseer": "yeoman-overseer",
}


def canonical_service(name: str) -> str:
    """Map a short name or ``*.service`` unit name to the store key."""
    name = name.strip()
    name = name.removesuffix(".service")
    return SERVICE_ALIASES.get(name, name)


def systemd_unit(service: str) -> str | None:
    """The systemd unit behind a store key, or None for an in-process channel."""
    if "/" in service:
        return None
    return f"{service}.service"


@dataclass(frozen=True, slots=True)
class DesiredRecord:
    desired: Desired
    until: float | None = None
    reason: str = ""
    set_by: str = ""
    set_at: float = 0.0


def default_path() -> Path:
    return get_operational_store_path("desired_state")


class DesiredStateStore:
    def __init__(self, path: Path | None = None, *, clock: Callable[[], float] = time.time) -> None:
        self.path = path or default_path()
        self._clock = clock

    def _read_raw(self) -> dict[str, dict[str, object]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except OSError, ValueError:
            return {}
        return raw if isinstance(raw, dict) else {}

    def get(self, service: str) -> DesiredRecord:
        """The effective record; an expired pause reads as running."""
        raw = self._read_raw().get(canonical_service(service))
        if not isinstance(raw, dict):
            return DesiredRecord("running")
        try:
            record = DesiredRecord(
                desired=raw["desired"],  # type: ignore[arg-type]
                until=None if raw.get("until") is None else float(raw["until"]),  # type: ignore[arg-type]
                reason=str(raw.get("reason", "")),
                set_by=str(raw.get("set_by", "")),
                set_at=float(raw.get("set_at", 0.0)),  # type: ignore[arg-type]
            )
        except KeyError, TypeError, ValueError:
            return DesiredRecord("running")
        if record.desired not in ("running", "paused", "stopped"):
            return DesiredRecord("running")
        if (
            record.desired == "paused"
            and record.until is not None
            and self._clock() >= record.until
        ):
            return DesiredRecord("running", reason="pause expired")
        return record

    def is_held(self, service: str) -> bool:
        """True when the owner (or a give-up) wants this service left alone."""
        return self.get(service).desired != "running"

    def all(self) -> dict[str, DesiredRecord]:
        return {name: self.get(name) for name in self._read_raw()}

    def set(
        self,
        service: str,
        desired: Desired,
        *,
        until: float | None = None,
        reason: str = "",
        set_by: str = "cli",
    ) -> DesiredRecord:
        key = canonical_service(service)
        record = DesiredRecord(desired, until, reason, set_by, self._clock())
        with self._locked():
            raw = self._read_raw()
            if desired == "running":
                raw.pop(key, None)
            else:
                raw[key] = asdict(record)
            self._write(raw)
        return record

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(self.path.name + ".lock")
        with open(lock_path, "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def _write(self, raw: dict[str, object]) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(raw, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise


def parse_duration(text: str) -> float:
    """``90s``, ``30m``, ``12h``, ``2d`` → seconds."""
    text = text.strip().lower()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if not text or text[-1] not in units:
        raise ValueError(f"duration needs a unit (s, m, h, d): {text!r}")
    value = float(text[:-1])
    if value <= 0:
        raise ValueError(f"duration must be positive: {text!r}")
    return value * units[text[-1]]
