"""Browse tool: real browser control via pinchtab HTTP API."""

import json
import os
from pathlib import Path
from typing import Any

import httpx

from yeoman.agent.tools.base import Tool

_DEFAULT_URL = "http://localhost:9867"
_TOKEN_FILE = Path.home() / ".yeoman" / "workspace" / "skills" / "browser" / "state" / "bridge_token.txt"
_TIMEOUT = 30.0


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

        try:
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

        except httpx.ConnectError:
            return (
                "Error: Cannot connect to pinchtab. "
                "Start it with: bash ~/.yeoman/workspace/skills/browser/scripts/start.sh &"
            )
        except httpx.TimeoutException:
            return "Error: Request to pinchtab timed out."
        except httpx.HTTPError as exc:
            return f"Error: HTTP request failed: {exc}"
