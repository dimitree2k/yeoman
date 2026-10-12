"""Decode plain or fenced model JSON responses."""
from __future__ import annotations

import json
from typing import Any


def _extract_json(text: str) -> Any:
    stripped = text.strip()
    fenced = stripped
    if stripped.startswith("```"):
        parts = stripped.split("```")
        if len(parts) >= 2:
            fenced = parts[1]
            if fenced.lstrip().lower().startswith("json"):
                fenced = fenced.lstrip()[4:]
    try:
        return json.loads(fenced.strip())
    except (json.JSONDecodeError, ValueError):
        return None

