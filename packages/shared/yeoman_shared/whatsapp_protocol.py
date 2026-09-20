"""Shared constants for the WhatsApp bridge wire protocol."""

PROTOCOL_VERSION = 5
"""Bridge v5 carries authenticated replay metadata and complete WhatsApp payload shapes."""

MAX_BRIDGE_FRAME_BYTES = 262_144
"""UTF-8 serialized event and WebSocket frame ceiling shared with the Bridge."""

REPLAYABLE_EVENT_TYPES = frozenset({"message", "edit", "delete", "reaction", "receipt"})
"""Business event kinds retained by the Bridge outbox."""

MEDIA_METADATA_FIELDS = frozenset(
    {"kind", "mimeType", "fileName", "bytes", "path", "ref", "sha256", "hash"}
)
"""Allowlisted media metadata; binary content is never part of a wire envelope."""

OUTBOUND_MESSAGE_ID_FIELDS = frozenset({"providerMessageId", "clientMessageId"})
"""Separate provider acknowledgement identity from the caller's idempotency id."""
