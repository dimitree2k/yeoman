"""Identifier normalization for the person directory (phones as E.164 digits, LIDs as digits)."""

from __future__ import annotations

import re
from dataclasses import dataclass

_PHONE = re.compile(r"^[1-9][0-9]{6,14}$")
_PHONE_NOISE = re.compile(r"[\s\-/().]")
_DIGITS = re.compile(r"^[0-9]+$")
_DEVICE = re.compile(r":\d+(?=@)")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_TELEGRAM_USERNAME = re.compile(r"^[A-Za-z0-9_]{5,32}$")


@dataclass(frozen=True, slots=True)
class ObservedId:
    kind: str
    value: str
    valid_from_ms: int | None = None
    valid_until_ms: int | None = None
    first_seen_ms: int | None = None
    last_seen_ms: int | None = None


def normalize_phone(raw: str) -> str | None:
    """Return E.164 digits without '+', or None when the country code is unknown or invalid."""
    if not isinstance(raw, str):
        return None
    text = _PHONE_NOISE.sub("", raw.strip())
    if text.startswith("+"):
        text = text[1:]
    elif text.startswith("00"):
        text = text[2:]
    elif text.startswith("0"):
        return None                     # national form: country code unknown
    return text if _PHONE.match(text) else None


def _jid_digits(value: str, suffix: str) -> str | None:
    low = _DEVICE.sub("", value.strip().lower())
    if not low.endswith(suffix):
        return None
    digits = low[: -len(suffix)]
    return digits if _DIGITS.match(digits) else None


def identifier_from_history(kind: str, value: str, *, valid_from_ms: int | None = None,
                            valid_until_ms: int | None = None, first_seen_ms: int | None = None,
                            last_seen_ms: int | None = None) -> ObservedId | None:
    """Map a history identifier_history row to a directory identifier, or None."""
    if not isinstance(value, str):
        return None
    if kind == "pn_jid":
        digits = _jid_digits(value, "@s.whatsapp.net")
        mapped = ("phone", digits) if digits and _PHONE.match(digits) else None
    elif kind == "lid":
        digits = _jid_digits(value, "@lid")
        mapped = ("wa_lid", digits) if digits else None
    elif kind == "numeric":
        phone = normalize_phone(value)
        mapped = ("phone", phone) if phone else None
    else:                               # newsletter, push_name and anything unknown
        mapped = None
    if mapped is None:
        return None
    return ObservedId(mapped[0], mapped[1], valid_from_ms, valid_until_ms, first_seen_ms, last_seen_ms)


def is_valid_value(kind: str, value: str) -> bool:
    if not isinstance(value, str):
        return False
    if kind == "phone":
        return bool(_PHONE.match(value))
    if kind in ("wa_lid", "telegram_id"):
        return bool(_DIGITS.match(value))
    if kind == "email":
        return bool(_EMAIL.match(value))
    if kind == "telegram_username":
        return bool(_TELEGRAM_USERNAME.match(value))
    return bool(value.strip())
