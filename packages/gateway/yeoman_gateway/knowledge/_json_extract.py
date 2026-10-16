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



def _extract_json_rows(text: str, key: str) -> list[Any] | None:
    """Read the complete objects of a ``{key: [...]}`` list, even if the text is cut off.

    A model that hits its token limit stops mid-row; every row before that is still
    whole and worth keeping.  Prose around the JSON is ignored.  Returns ``None`` when
    no such list can be found at all.
    """
    marker = text.find(f'"{key}"')
    start = text.find("[", marker if marker >= 0 else 0)
    if start < 0:
        return None
    decoder = json.JSONDecoder()
    rows: list[Any] = []
    position = start + 1
    while True:
        while position < len(text) and text[position] in " \t\r\n,":
            position += 1
        if position >= len(text) or text[position] == "]":
            return rows
        try:
            row, position = decoder.raw_decode(text, position)
        except (json.JSONDecodeError, ValueError):
            return rows
        rows.append(row)
