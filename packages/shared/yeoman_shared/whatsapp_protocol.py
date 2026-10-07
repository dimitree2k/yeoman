"""Shared constants for the WhatsApp bridge wire protocol."""

PROTOCOL_VERSION = 5
"""Bridge v5 carries authenticated replay metadata and complete WhatsApp payload shapes."""

MAX_BRIDGE_FRAME_BYTES = 262_144
"""UTF-8 serialized event and WebSocket frame ceiling shared with the Bridge."""

REPLAYABLE_EVENT_TYPES = frozenset(
    {"message", "edit", "delete", "reaction", "receipt", "membership_change", "membership_snapshot"}
)
"""Business event kinds retained by the Bridge outbox."""

MEDIA_METADATA_FIELDS = frozenset(
    {"kind", "mimeType", "fileName", "bytes", "path", "ref", "sha256", "hash"}
)
"""Allowlisted media metadata; binary content is never part of a wire envelope."""

OUTBOUND_MESSAGE_ID_FIELDS = frozenset({"providerMessageId", "clientMessageId"})
"""Separate provider acknowledgement identity from the caller's idempotency id."""


# ECMAScript String.trim whitespace, shared with the Bridge's asString/normalizeJid.
BRIDGE_WHITESPACE = "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
JS_MAX_SAFE_INTEGER = 9007199254740991


def bridge_string(value: object) -> str | None:
    return value.strip(BRIDGE_WHITESPACE) or None if isinstance(value, str) else None


def _js_safe_integer(value: object) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and -JS_MAX_SAFE_INTEGER <= value <= JS_MAX_SAFE_INTEGER
            and value % 1 == 0)


def normalize_whatsapp_jid(value: str) -> str:
    """Mirror Bridge normalizeJid: trim, then remove the device suffix before @."""
    left, *rest = (bridge_string(value) or "").split("@", 2)
    left = left.split(":", 1)[0]
    right = rest[0] if rest else ""
    return f"{left}@{right}" if right else left


# Additive v5 result contracts. Older archives may omit poll/content.
def valid_poll_result(value: object) -> bool:
    return (isinstance(value, dict) and set(value) == {"name", "values", "selectableCount"}
            and bridge_string(value["name"]) is not None
            and len(value["name"].encode("utf-16-le", errors="surrogatepass")) <= 1024
            and isinstance(value["values"], list) and 2 <= len(value["values"]) <= 12
            and all(bridge_string(option) is not None for option in value["values"])
            and _js_safe_integer(value["selectableCount"]) and 1 <= value["selectableCount"] <= 12)


def valid_forward_content(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != FORWARD_CONTENT_FIELDS:
        return False
    if not all(value[key] is None or isinstance(value[key], str) for key in ("text", "caption")):
        return False
    if (value["forwarded"] is not True or not isinstance(value["provenance"], str)
            or value["provenance"] not in ("source", "sent")
            or not all(bridge_string(value[k]) is not None for k in ("sourceChatJid", "sourceMessageId"))):
        return False
    media = value["media"]
    return media is None or (isinstance(media, dict) and set(media) <= MEDIA_METADATA_FIELDS
                             and media.get("kind") in ("image", "video", "audio", "document", "sticker")
                             and all(_js_safe_integer(v) and v >= 0 if k == "bytes" else isinstance(v, str)
                                     for k, v in media.items()))


POLL_RESULT_FIELDS = frozenset({"name", "values", "selectableCount"})
FORWARD_CONTENT_FIELDS = frozenset(
    {"text", "caption", "media", "forwarded", "sourceChatJid", "sourceMessageId", "provenance"}
)
"""Forward provenance identifies the body used: provider sent body or original source fallback."""
