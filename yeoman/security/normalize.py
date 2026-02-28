"""Normalization helpers for security checks."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_ZERO_WIDTH = {
    "\u200b",  # zero width space
    "\u200c",  # zero width non-joiner
    "\u200d",  # zero width joiner
    "\ufeff",  # byte order mark
    "\u2060",  # word joiner
    "\u00ad",  # soft hyphen
}


@dataclass(frozen=True, slots=True)
class NormalizedText:
    """Precomputed normalized views of one text payload."""

    original: str
    lowered: str
    compact: str


def normalize_text(text: str) -> NormalizedText:
    """Normalize text to reduce simple obfuscation tricks.

    - Unicode NFKC canonicalization
    - zero-width character removal
    - whitespace collapsing
    - lowercase view
    - compact view without separators for split-token bypasses
    """
    raw = text or ""
    normalized = unicodedata.normalize("NFKC", raw)
    normalized = "".join(ch for ch in normalized if ch not in _ZERO_WIDTH)
    normalized = re.sub(r"\s+", " ", normalized).strip()

    lowered = normalized.lower()
    compact = re.sub(r"[\s\-+_`'\".,:;|/\\]+", "", lowered)
    return NormalizedText(original=raw, lowered=lowered, compact=compact)
