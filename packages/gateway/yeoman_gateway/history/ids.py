"""Classify WhatsApp identifiers by their suffix (owner rules, spec 2026-10-05)."""

from __future__ import annotations

import re
from dataclasses import dataclass

PN_SUFFIX = "@s.whatsapp.net"
LID_SUFFIX = "@lid"
GROUP_SUFFIX = "@g.us"
NEWSLETTER_SUFFIX = "@newsletter"
SPEAKUP = "service:speakup"
STRONG_KINDS = frozenset({"lid", "pn_jid", "newsletter"})

_DEVICE = re.compile(r":\d+(?=@)")
_DIGITS = re.compile(r"^\d{5,}$")
_OLD_GROUP = re.compile(r"^\d{5,}-\d{5,}$")


@dataclass(frozen=True, order=True)
class Ident:
    kind: str
    value: str

    @property
    def strong(self) -> bool:
        return self.kind in STRONG_KINDS


def classify(raw: object) -> Ident | None:
    """Return the identifier's kind and canonical value, or None when empty."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if text == SPEAKUP:
        return Ident("assistant", SPEAKUP)
    low = _DEVICE.sub("", text.lower())
    if low.endswith(LID_SUFFIX):
        return Ident("lid", low)
    if low.endswith(PN_SUFFIX):
        return Ident("pn_jid", low)
    if low.endswith("@c.us"):
        return Ident("pn_jid", low.removesuffix("@c.us") + PN_SUFFIX)
    if low.endswith(NEWSLETTER_SUFFIX):
        return Ident("newsletter", low)
    if low.endswith(GROUP_SUFFIX):
        return Ident("group", low)
    if _OLD_GROUP.match(low):
        return Ident("group", low + GROUP_SUFFIX)
    if low.startswith("+") and _DIGITS.match(low[1:]):
        return Ident("numeric", low[1:])
    if _DIGITS.match(low):
        return Ident("numeric", low)
    return Ident("other", text)


def numeric_part(value: str) -> str:
    """Return the identifier portion before its JID suffix."""
    return value.split("@", 1)[0]
