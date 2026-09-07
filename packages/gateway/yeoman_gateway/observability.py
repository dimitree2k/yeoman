"""Helpers for safe, low-cardinality operational log fields."""

from __future__ import annotations

import hashlib

_CONTROL_ESCAPE = "\\x"


def safe_log_token(value: object, *, max_length: int = 160) -> str:
    """Bound and escape a value before placing it in a log field."""
    text = str(value or "")
    escaped: list[str] = []
    for char in text:
        codepoint = ord(char)
        if codepoint < 0x20 or codepoint == 0x7F:
            escaped.append(f"{_CONTROL_ESCAPE}{codepoint:02x}")
        else:
            escaped.append(char)
    return "".join(escaped)[:max(1, max_length)]


def private_log_identifier(value: object, *, max_length: int = 24) -> str:
    """Return a stable non-reversible identifier for a private value."""
    text = str(value or "")
    if not text:
        return ""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"[:max(1, max_length)]
