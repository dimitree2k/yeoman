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
            "Read Raspberry Pi/system stats (temperature, CPU, memory, disk, uptime, top processes) "
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
                "include_top_processes": {
                    "type": "boolean",
                    "description": "Include top processes by CPU usage. Defaults to true.",
                },
                "top_n": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "description": "Max number of processes in top_processes. Defaults to 8.",
                },
            },
            "required": [],
        }

    async def execute(
        self,
        format: str = "text",
        include_top_processes: bool = True,
        top_n: int = 8,
        **kwargs: Any,
    ) -> str:
        del kwargs
        stats = await self._collect_stats(
            include_top_processes=bool(include_top_processes),
            top_n=max(1, min(int(top_n), 20)),
        )
        if format == "json":
            return json.dumps(stats, ensure_ascii=False, indent=2)
        return self._to_text(stats)

    async def _collect_stats(self, *, include_top_processes: bool, top_n: int) -> dict[str, Any]:
        cpu_usage_pct = await self._cpu_usage_percent()
        mem_total_mb, mem_available_mb = self._meminfo()
        disk_total_gb, disk_used_gb, disk_free_gb = self._disk_root()
        top_processes = await self._top_processes(top_n=top_n) if include_top_processes else []

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
            "top_processes": top_processes,
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

    async def _top_processes(self, *, top_n: int) -> list[dict[str, Any]]:
        total_mem_bytes = self._mem_total_bytes()
        total_first = self._read_proc_cpu_total()
        snap_first = self._process_snapshot()
        if total_first is None or not snap_first:
            return []

        await asyncio.sleep(0.2)
        total_second = self._read_proc_cpu_total()
        snap_second = self._process_snapshot()
        if total_second is None or not snap_second:
            return []

        total_delta = total_second - total_first
        if total_delta <= 0:
            return []

        rows: list[dict[str, Any]] = []
        for pid, proc2 in snap_second.items():
            proc1 = snap_first.get(pid)
            if proc1 is None:
                continue
            cpu_delta = proc2["cpu_jiffies"] - proc1["cpu_jiffies"]
            if cpu_delta < 0:
                continue
            cpu_pct = cpu_delta / total_delta * 100.0
            rss_bytes = proc2["rss_bytes"]
            mem_pct = (rss_bytes / total_mem_bytes * 100.0) if total_mem_bytes and total_mem_bytes > 0 else None
            rows.append({
                "pid": pid,
                "ppid": proc2["ppid"],
                "command": proc2["command"],
                "cpu_pct": round(cpu_pct, 2),
                "rss_mb": round(rss_bytes / (1024.0 * 1024.0), 2),
                "mem_pct": round(mem_pct, 2) if mem_pct is not None else None,
            })

        rows.sort(key=lambda p: p["cpu_pct"], reverse=True)
        return rows[:top_n]

    def _process_snapshot(self) -> dict[int, dict[str, Any]]:
        page_size = os.sysconf("SC_PAGE_SIZE")
        out: dict[int, dict[str, Any]] = {}
        proc_root = Path("/proc")
        try:
            entries = list(proc_root.iterdir())
        except OSError:
            return out

        for entry in entries:
            name = entry.name
            if not name.isdigit():
                continue
            pid = int(name)
            try:
                stat_line = (entry / "stat").read_text(encoding="utf-8").strip()
                rparen = stat_line.rfind(")")
                if rparen == -1:
                    continue
                lparen = stat_line.find("(")
                if lparen == -1:
                    continue
                command = stat_line[lparen + 1:rparen]
                rest = stat_line[rparen + 2:].split()
                if len(rest) <= 21:
                    continue

                ppid = int(rest[1])
                utime = int(rest[11])
                stime = int(rest[12])
                rss_pages = int(rest[21])
                rss_bytes = max(0, rss_pages) * page_size

                out[pid] = {
                    "ppid": ppid,
                    "command": command,
                    "cpu_jiffies": utime + stime,
                    "rss_bytes": rss_bytes,
                }
            except (OSError, ValueError, IndexError):
                continue
        return out

    def _read_proc_cpu_total(self) -> int | None:
        path = Path("/proc/stat")
        if not path.exists():
            return None
        try:
            first_line = path.read_text(encoding="utf-8").splitlines()[0]
            parts = first_line.split()
            if len(parts) < 2 or parts[0] != "cpu":
                return None
            return sum(int(v) for v in parts[1:])
        except (OSError, ValueError, IndexError):
            return None

    def _mem_total_bytes(self) -> int | None:
        path = Path("/proc/meminfo")
        if not path.exists():
            return None
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            return None
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
        top_processes = stats.get("top_processes") or []
        lines.append("- top_processes:")
        if not top_processes:
            lines.append("  - none")
        else:
            for proc in top_processes:
                lines.append(
                    "  - "
                    f"pid={proc.get('pid')} "
                    f"cpu_pct={proc.get('cpu_pct')} "
                    f"mem_pct={proc.get('mem_pct')} "
                    f"rss_mb={proc.get('rss_mb')} "
                    f"cmd={proc.get('command')}"
                )
        return "\n".join(lines)
