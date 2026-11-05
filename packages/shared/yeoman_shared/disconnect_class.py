"""Classify channel disconnects so callers know whether a retry can help.

``transient``      retry within the connect budget
``needs_restart``  reconnect once right away (e.g. WhatsApp 515 after pairing)
``needs_human``    no automatic login attempt can fix it (logged out, banned, revoked token)
"""

from __future__ import annotations

from typing import Literal

DisconnectClass = Literal["transient", "needs_restart", "needs_human"]

# Baileys DisconnectReason codes (Boom statusCode on connection close).
WHATSAPP_CODES: dict[int, tuple[str, DisconnectClass]] = {
    401: ("logged_out", "needs_human"),
    403: ("forbidden", "needs_human"),
    408: ("timed_out", "transient"),
    411: ("multidevice_mismatch", "needs_human"),
    428: ("connection_closed", "transient"),
    440: ("connection_replaced", "transient"),
    500: ("bad_session", "needs_human"),
    503: ("unavailable_service", "transient"),
    515: ("restart_required", "needs_restart"),
}

# Telegram Bot API error codes.
TELEGRAM_CODES: dict[int, tuple[str, DisconnectClass]] = {
    401: ("unauthorized", "needs_human"),
    404: ("token_invalid", "needs_human"),
    409: ("conflict", "transient"),
    429: ("rate_limited", "transient"),
}

_TABLES = {"whatsapp": WHATSAPP_CODES, "telegram": TELEGRAM_CODES}


def classify_disconnect(
    channel: str,
    code: int | None,
    overrides: dict[int, DisconnectClass] | None = None,
) -> tuple[str, DisconnectClass]:
    """Return ``(reason, class)`` for a channel close code; unknown codes are transient."""
    if code is None:
        return ("unknown", "transient")
    reason, cls = _TABLES.get(channel, {}).get(code, ("unknown", "transient"))
    if overrides and code in overrides:
        cls = overrides[code]
    return (reason, cls)
