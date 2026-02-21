"""Deep research tool wrapper around the local deep-research skill script."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool


class DeepResearchTool(Tool):
    """Run multi-pass web research via the installed deep-research script."""

    name = "deep_research"
    description = (
        "Run deep web research using the local deep-research skill and return a synthesized report."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Research question"},
            "depth": {
                "type": "string",
                "enum": ["basic", "advanced"],
                "description": "Research depth",
                "default": "advanced",
            },
            "max_results": {
                "type": "integer",
                "description": "Max results per search pass (1-20)",
                "minimum": 1,
                "maximum": 20,
            },
            "single": {
                "type": "boolean",
                "description": "Single pass only (faster, less comprehensive)",
                "default": False,
            },
            "format": {
                "type": "string",
                "description": "Optional response structure spec",
            },
        },
        "required": ["query"],
    }

    def __init__(self, script_path: str | None = None, timeout: int = 180):
        default_path = (
            Path.home() / ".nanobot" / "workspace" / "skills" / "deep-research" / "scripts" / "research.py"
        )
        configured = os.environ.get("NANOBOT_DEEP_RESEARCH_SCRIPT", "").strip()
        self.script_path = Path(script_path or configured or default_path).expanduser()
        self.timeout = max(10, int(timeout))

    async def execute(
        self,
        query: str,
        depth: str = "advanced",
        max_results: int | None = None,
        single: bool = False,
        format: str | None = None,
        **kwargs: Any,
    ) -> str:
        if not query.strip():
            return "Error: query cannot be empty"
        if not self.script_path.exists():
            return f"Error: deep-research script not found at {self.script_path}"

        cmd = ["python3", str(self.script_path), "--query", query, "--depth", depth]
        if max_results is not None:
            cmd.extend(["--max-results", str(max_results)])
        if single:
            cmd.append("--single")
        if format:
            cmd.extend(["--format", format])

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
            except asyncio.TimeoutError:
                proc.kill()
                return f"Error: deep_research timed out after {self.timeout} seconds"

            out = stdout.decode("utf-8", errors="replace").strip()
            err = stderr.decode("utf-8", errors="replace").strip()

            if proc.returncode != 0:
                msg = out or err or "unknown error"
                return f"Error: deep_research failed (exit {proc.returncode}): {msg}"

            if out:
                return out
            if err:
                return err
            return "(no output)"
        except Exception as e:
            return f"Error executing deep_research: {e}"

