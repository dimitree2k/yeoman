"""Read-only Raspberry Pi and host system stats tool."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool


class PiStatsTool(Tool):
    """Expose safe read-only host metrics without shell execution."""

    @property
    def name(self) -> str:
        return "pi_stats"

    @property
    def description(self) -> str:
        return (
            "Read Raspberry Pi/system stats (temperature, CPU, memory, disk, uptime) "
            "without shell commands."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "format": {
                    "type": "string",
                    "enum": ["text", "json"],
                    "description": "Output format. Defaults to text.",
                },
            },
            "required": [],
        }

    async def execute(self, format: str = "text", **kwargs: Any) -> str:
        del kwargs
        stats = await self._collect_stats()
        if format == "json":
            return json.dumps(stats, ensure_ascii=False, indent=2)
        return self._to_text(stats)

    async def _collect_stats(self) -> dict[str, Any]:
        cpu_usage_pct = await self._cpu_usage_percent()
        mem_total_mb, mem_available_mb = self._meminfo()
        disk_total_gb, disk_used_gb, disk_free_gb = self._disk_root()

        return {
            "temperature_c": self._cpu_temperature_c(),
            "cpu_usage_pct": cpu_usage_pct,
            "loadavg_1m": self._loadavg_1m(),
            "memory_total_mb": mem_total_mb,
            "memory_available_mb": mem_available_mb,
            "memory_used_mb": (
                (mem_total_mb - mem_available_mb)
                if mem_total_mb is not None and mem_available_mb is not None
                else None
            ),
            "disk_root_total_gb": disk_total_gb,
            "disk_root_used_gb": disk_used_gb,
            "disk_root_free_gb": disk_free_gb,
            "uptime_seconds": self._uptime_seconds(),
        }

    def _cpu_temperature_c(self) -> float | None:
        path = Path("/sys/class/thermal/thermal_zone0/temp")
        if not path.exists():
            return None
        try:
            raw = path.read_text(encoding="utf-8").strip()
            value = float(raw)
        except (OSError, ValueError):
            return None
        if value > 1000:
            value /= 1000.0
        return round(value, 2)

    async def _cpu_usage_percent(self) -> float | None:
        first = self._read_proc_stat_cpu()
        if first is None:
            return None
        await asyncio.sleep(0.2)
        second = self._read_proc_stat_cpu()
        if second is None:
            return None

        first_idle, first_total = first
        second_idle, second_total = second
        delta_total = second_total - first_total
        delta_idle = second_idle - first_idle
        if delta_total <= 0:
            return None
        usage = (delta_total - delta_idle) / delta_total * 100.0
        return round(usage, 2)

    def _read_proc_stat_cpu(self) -> tuple[int, int] | None:
        path = Path("/proc/stat")
        if not path.exists():
            return None
        try:
            first_line = path.read_text(encoding="utf-8").splitlines()[0]
            parts = first_line.split()
            if len(parts) < 5 or parts[0] != "cpu":
                return None
            values = [int(v) for v in parts[1:]]
        except (OSError, ValueError, IndexError):
            return None

        total = sum(values)
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        return idle, total

    def _meminfo(self) -> tuple[float | None, float | None]:
        path = Path("/proc/meminfo")
        if not path.exists():
            return None, None

        mem_total_kb: int | None = None
        mem_available_kb: int | None = None
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith("MemTotal:"):
                    mem_total_kb = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    mem_available_kb = int(line.split()[1])
        except (OSError, ValueError, IndexError):
            return None, None

        if mem_total_kb is None:
            return None, None
        if mem_available_kb is None:
            mem_available_kb = 0
        return round(mem_total_kb / 1024.0, 2), round(mem_available_kb / 1024.0, 2)

    def _disk_root(self) -> tuple[float | None, float | None, float | None]:
        try:
            stat = os.statvfs("/")
        except OSError:
            return None, None, None

        total = stat.f_blocks * stat.f_frsize
        free = stat.f_bavail * stat.f_frsize
        used = total - free
        gb = 1024.0**3
        return round(total / gb, 2), round(used / gb, 2), round(free / gb, 2)

    def _uptime_seconds(self) -> int | None:
        path = Path("/proc/uptime")
        if not path.exists():
            return None
        try:
            raw = path.read_text(encoding="utf-8").split()[0]
            return int(float(raw))
        except (OSError, ValueError, IndexError):
            return None

    def _loadavg_1m(self) -> float | None:
        path = Path("/proc/loadavg")
        if not path.exists():
            return None
        try:
            raw = path.read_text(encoding="utf-8").split()[0]
            return round(float(raw), 2)
        except (OSError, ValueError, IndexError):
            return None

    @staticmethod
    def _to_text(stats: dict[str, Any]) -> str:
        lines = [
            "Raspberry Pi Stats",
            f"- temperature_c: {stats.get('temperature_c')}",
            f"- cpu_usage_pct: {stats.get('cpu_usage_pct')}",
            f"- loadavg_1m: {stats.get('loadavg_1m')}",
            f"- memory_total_mb: {stats.get('memory_total_mb')}",
            f"- memory_used_mb: {stats.get('memory_used_mb')}",
            f"- memory_available_mb: {stats.get('memory_available_mb')}",
            f"- disk_root_total_gb: {stats.get('disk_root_total_gb')}",
            f"- disk_root_used_gb: {stats.get('disk_root_used_gb')}",
            f"- disk_root_free_gb: {stats.get('disk_root_free_gb')}",
            f"- uptime_seconds: {stats.get('uptime_seconds')}",
        ]
        return "\n".join(lines)
