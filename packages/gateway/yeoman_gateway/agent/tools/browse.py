"""Browse tool: real browser control via pinchtab HTTP API."""

import asyncio
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from yeoman_gateway.agent.tools.base import Tool

_DEFAULT_URL = "http://localhost:9867"
_SKILL_DIR = Path(__file__).resolve().parents[2] / "skills" / "browser"
_TOKEN_FILE = _SKILL_DIR / "state" / "bridge_token.txt"
_START_SCRIPT = _SKILL_DIR / "scripts" / "start.sh"
_TIMEOUT = 30.0
_AUTOSTART_HEALTH_TIMEOUT = 10.0
_AUTOSTART_RETRY_COOLDOWN = 30.0
_autostart_last_attempt = 0.0
_autostart_lock = asyncio.Lock()


def _base_url() -> str:
    return os.environ.get("PINCHTAB_URL", _DEFAULT_URL)


def _token() -> str:
    tok = os.environ.get("BRIDGE_TOKEN", "")
    if not tok and _TOKEN_FILE.is_file():
        tok = _TOKEN_FILE.read_text().strip()
    return tok


def _headers() -> dict[str, str]:
    h: dict[str, str] = {"Content-Type": "application/json"}
    tok = _token()
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


async def _wait_for_health(base: str, headers: dict[str, str], timeout: float) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    async with httpx.AsyncClient(timeout=2.0) as client:
        while asyncio.get_event_loop().time() < deadline:
            try:
                r = await client.get(f"{base}/health", headers=headers)
                if r.status_code == 200:
                    return True
            except (httpx.ConnectError, httpx.TimeoutException):
                pass
            await asyncio.sleep(0.4)
    return False


async def _try_autostart(base: str, headers: dict[str, str]) -> bool:
    """Spawn pinchtab via start.sh (detached) and wait for /health.

    Returns True if pinchtab is reachable after the attempt. Debounced via
    a process-wide cooldown so repeated tool calls don't fork-bomb the script.
    """
    global _autostart_last_attempt
    if not _START_SCRIPT.is_file():
        logger.warning("pinchtab autostart skipped: {} not found", _START_SCRIPT)
        return False

    async with _autostart_lock:
        now = time.monotonic()
        if now - _autostart_last_attempt < _AUTOSTART_RETRY_COOLDOWN:
            return await _wait_for_health(base, headers, 1.0)
        _autostart_last_attempt = now

        log_dir = Path.home() / ".yeoman" / "var" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "pinchtab.log"
        logger.info("pinchtab autostart: launching {}", _START_SCRIPT)
        try:
            with log_path.open("ab") as logf:
                subprocess.Popen(  # noqa: S603 - script path is package-bundled
                    ["bash", str(_START_SCRIPT)],
                    stdout=logf,
                    stderr=logf,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                    close_fds=True,
                )
        except OSError as exc:
            logger.warning("pinchtab autostart failed to spawn: {}", exc)
            return False

        ok = await _wait_for_health(base, headers, _AUTOSTART_HEALTH_TIMEOUT)
        logger.info("pinchtab autostart {}", "ok" if ok else "timed out")
        return ok


class BrowseTool(Tool):
    """Navigate and interact with web pages via pinchtab browser automation."""

    @property
    def name(self) -> str:
        return "browse"

    @property
    def description(self) -> str:
        return (
            "Control a real Chrome browser via pinchtab. "
            "Navigate to URLs, read page content as text, interact with elements "
            "(click, fill, press), take snapshots, or run JavaScript. "
            "Session-persistent: logged-in sites stay logged in."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "navigate",
                        "read",
                        "snapshot",
                        "click",
                        "fill",
                        "press",
                        "evaluate",
                        "health",
                    ],
                    "description": (
                        "Action to perform. "
                        "navigate: go to a URL. "
                        "read: get page text (~800 tokens). "
                        "snapshot: get accessibility tree (supports filter/diff). "
                        "click/fill/press: interact with elements. "
                        "evaluate: run JavaScript. "
                        "health: check if pinchtab is running."
                    ),
                },
                "url": {
                    "type": "string",
                    "description": "URL to navigate to (required for 'navigate' action).",
                },
                "selector": {
                    "type": "string",
                    "description": "CSS selector for click/fill actions.",
                },
                "value": {
                    "type": "string",
                    "description": "Text to type (required for 'fill' action).",
                },
                "key": {
                    "type": "string",
                    "description": "Key to press, e.g. 'Enter' (required for 'press' action).",
                },
                "expression": {
                    "type": "string",
                    "description": "JavaScript expression to evaluate.",
                },
                "filter": {
                    "type": "string",
                    "enum": ["interactive", "text"],
                    "description": "Snapshot filter: 'interactive' for buttons/links/inputs only, 'text' for plain text.",
                },
                "diff": {
                    "type": "boolean",
                    "description": "If true, snapshot returns only what changed since last call.",
                },
            },
            "required": ["action"],
        }

    async def execute(
        self,
        action: str,
        url: str | None = None,
        selector: str | None = None,
        value: str | None = None,
        key: str | None = None,
        expression: str | None = None,
        filter: str | None = None,
        diff: bool = False,
        **kwargs: Any,
    ) -> str:
        base = _base_url()
        headers = _headers()

        for attempt in range(2):
            try:
                return await self._dispatch(
                    base, headers, action, url, selector, value, key, expression, filter, diff
                )
            except httpx.ConnectError:
                if attempt == 0 and await _try_autostart(base, headers):
                    continue
                return (
                    "Error: pinchtab is not reachable and autostart did not succeed. "
                    f"Check {Path.home()}/.yeoman/var/logs/pinchtab.log for details, "
                    f"or run: bash {_START_SCRIPT} &"
                )
            except httpx.TimeoutException:
                return "Error: Request to pinchtab timed out."
            except httpx.HTTPError as exc:
                return f"Error: HTTP request failed: {exc}"

        return "Error: pinchtab unreachable."

    async def _dispatch(
        self,
        base: str,
        headers: dict[str, str],
        action: str,
        url: str | None,
        selector: str | None,
        value: str | None,
        key: str | None,
        expression: str | None,
        filter: str | None,
        diff: bool,
    ) -> str:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            if action == "health":
                r = await client.get(f"{base}/health", headers=headers)
                return r.text

            if action == "navigate":
                if not url:
                    return "Error: 'url' parameter is required for navigate action."
                r = await client.post(
                    f"{base}/navigate",
                    headers=headers,
                    json={"url": url},
                )
                return r.text

            if action == "read":
                r = await client.get(f"{base}/text", headers=headers)
                return r.text

            if action == "snapshot":
                params: dict[str, str] = {"format": "text"}
                if filter:
                    params["filter"] = filter
                if diff:
                    params["diff"] = "true"
                r = await client.get(
                    f"{base}/snapshot",
                    headers=headers,
                    params=params,
                )
                return r.text

            if action == "click":
                if not selector:
                    return "Error: 'selector' parameter is required for click action."
                r = await client.post(
                    f"{base}/action",
                    headers=headers,
                    json={"type": "click", "selector": selector},
                )
                return r.text

            if action == "fill":
                if not selector:
                    return "Error: 'selector' parameter is required for fill action."
                if value is None:
                    return "Error: 'value' parameter is required for fill action."
                r = await client.post(
                    f"{base}/action",
                    headers=headers,
                    json={"type": "fill", "selector": selector, "value": value},
                )
                return r.text

            if action == "press":
                if not key:
                    return "Error: 'key' parameter is required for press action."
                r = await client.post(
                    f"{base}/action",
                    headers=headers,
                    json={"type": "press", "key": key},
                )
                return r.text

            if action == "evaluate":
                if not expression:
                    return "Error: 'expression' parameter is required for evaluate action."
                r = await client.post(
                    f"{base}/evaluate",
                    headers=headers,
                    json={"expression": expression},
                )
                return r.text

            return f"Error: Unknown action '{action}'."
