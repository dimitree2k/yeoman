"""Unified, channel-agnostic message model.

Replaces the three-type system (WhatsApp local InboundEvent, bus InboundMessage,
core InboundEvent) with a single structured envelope.

Content blocks carry both raw and processed forms — e.g. an audio block holds
the filesystem path AND the ASR transcript, so downstream stages never need to
fish data out of untyped metadata dicts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

ContentKind = Literal["text", "image", "audio", "video", "sticker", "file"]


@dataclass(frozen=True, slots=True)
class ContentBlock:
    """One element of message content.

    A message may contain multiple blocks — e.g. a text caption plus an image.
    Media blocks carry both their raw path and any AI-processed result
    (transcript for audio, description for images/video/stickers).
    """

    kind: ContentKind
    text: str | None = None
    """Text body for ``text`` blocks, or caption for media blocks."""

    path: str | None = None
    """Filesystem path for media blocks."""

    mime_type: str | None = None
    """MIME type of the media (e.g. ``image/jpeg``, ``audio/ogg``)."""

    size_bytes: int | None = None
    """File size in bytes, if known."""

    transcript: str | None = None
    """ASR transcription output (populated for ``audio`` blocks)."""

    description: str | None = None
    """Vision/AI description (populated for ``image``, ``video``, ``sticker`` blocks)."""


@dataclass(frozen=True, slots=True)
class Identity:
    """Canonical sender representation.

    Wraps the platform-specific user ID with optional human-readable fields.
    """

    id: str
    """Platform-specific user identifier (e.g. WhatsApp JID, Telegram user_id)."""

    display_name: str | None = None
    """Human-readable display name, if available."""

    platform_handle: str | None = None
    """Platform handle (e.g. ``@username``, phone number)."""


@dataclass(frozen=True, slots=True)
class ReplyRef:
    """Reference to the message being replied to.

    Consolidates ``reply_to_message_id``, ``reply_to_text``, and
    ``reply_to_participant`` into one typed object.
    """

    message_id: str
    text: str | None = None
    """Quoted text of the original message."""

    sender: Identity | None = None
    """Who wrote the original message."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Message:
    """Channel-agnostic inbound message envelope.

    Produced by channel adapters, transported through the bus, and consumed
    by the pipeline/orchestrator.  All channel-specific data lives in typed
    fields or structured :class:`ContentBlock` instances — the ``metadata``
    dict is an escape hatch for genuinely channel-specific overflow only.
    """

    id: str | None = None
    """Unique message ID assigned by the platform."""

    channel: str
    """Channel name (``whatsapp``, ``telegram``, ``discord``, etc.)."""

    chat_id: str
    """Chat or group identifier."""

    sender: Identity
    """Who sent this message."""

    content: list[ContentBlock]
    """Ordered content elements (text, images, audio, etc.)."""

    reply_to: ReplyRef | None = None
    """If this message is a reply, what it's replying to."""

    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    is_group: bool = False
    """Whether the message was sent in a group chat."""

    mentioned_bot: bool = False
    """Whether the bot was explicitly @-mentioned."""

    reply_to_bot: bool = False
    """Whether this message is a reply to a bot message."""

    participant: str | None = None
    """WhatsApp group participant JID (kept for policy compatibility)."""

    metadata: dict[str, object] = field(default_factory=dict)
    """Channel-specific overflow — prefer typed fields where possible."""

    # ── Convenience helpers ──────────────────────────────────────────

    @property
    def session_key(self) -> str:
        """Unique key for session identification."""
        return f"{self.channel}:{self.chat_id}"

    @property
    def text_content(self) -> str:
        """Concatenated text from all content blocks (for backward compat)."""
        parts: list[str] = []
        for block in self.content:
            if block.kind == "text" and block.text:
                parts.append(block.text)
            elif block.transcript:
                parts.append(block.transcript)
            elif block.description:
                parts.append(f"[{block.kind}: {block.description}]")
        return "\n".join(parts)

    def normalized_text(self) -> str:
        """Stripped text content used for dedup and downstream processing."""
        return self.text_content.strip()

    @property
    def has_media(self) -> bool:
        """Whether any content block is a media type."""
        return any(b.kind != "text" for b in self.content)

    @property
    def media_paths(self) -> tuple[str, ...]:
        """Filesystem paths of all media blocks (backward compat with ``media`` tuple)."""
        return tuple(b.path for b in self.content if b.path)

    # ── Migration bridge ─────────────────────────────────────────────

    @classmethod
    def from_inbound_event(cls, event: object) -> Message:
        """Convert a legacy ``core.models.InboundEvent`` to a ``Message``.

        This enables incremental migration — call sites can switch one at a
        time without a big-bang rewrite.
        """
        # Avoid circular import — accept any object and duck-type.
        content_text = str(getattr(event, "content", "") or "").strip()
        content_blocks: list[ContentBlock] = []
        if content_text:
            content_blocks.append(ContentBlock(kind="text", text=content_text))

        # Migrate media tuple to content blocks.
        media = getattr(event, "media", ())
        for path in media:
            content_blocks.append(ContentBlock(kind="file", path=str(path)))

        sender_id = str(getattr(event, "sender_id", "") or "")

        reply_ref: ReplyRef | None = None
        reply_to_mid = getattr(event, "reply_to_message_id", None)
        if reply_to_mid:
            reply_sender: Identity | None = None
            reply_to_participant = getattr(event, "reply_to_participant", None)
            if reply_to_participant:
                reply_sender = Identity(id=str(reply_to_participant))
            reply_ref = ReplyRef(
                message_id=str(reply_to_mid),
                text=getattr(event, "reply_to_text", None),
                sender=reply_sender,
            )

        raw_metadata = dict(getattr(event, "raw_metadata", {}) or {})

        return cls(
            id=getattr(event, "message_id", None),
            channel=str(getattr(event, "channel", "") or ""),
            chat_id=str(getattr(event, "chat_id", "") or ""),
            sender=Identity(id=sender_id),
            content=content_blocks,
            reply_to=reply_ref,
            timestamp=getattr(event, "timestamp", datetime.now(UTC)),
            is_group=bool(getattr(event, "is_group", False)),
            mentioned_bot=bool(getattr(event, "mentioned_bot", False)),
            reply_to_bot=bool(getattr(event, "reply_to_bot", False)),
            participant=getattr(event, "participant", None),
            metadata=raw_metadata,
        )


def render_content_blocks(blocks: list[ContentBlock]) -> str:
    """Render content blocks into LLM-consumable text.

    Centralises the media-rendering logic that was previously scattered across
    channel adapters (each prepending ``[image_description]``, ``[transcription]``
    etc. in slightly different formats).
    """
    parts: list[str] = []
    for block in blocks:
        match block.kind:
            case "text":
                if block.text:
                    parts.append(block.text)
            case "image":
                if block.description:
                    parts.append(f"[Image: {block.description}]")
            case "audio":
                if block.transcript:
                    parts.append(f"[Voice message transcript: {block.transcript}]")
            case "video":
                if block.description:
                    parts.append(f"[Video: {block.description}]")
            case "sticker":
                if block.description:
                    parts.append(f"[Sticker: {block.description}]")
            case _:
                if block.text:
                    parts.append(block.text)
                elif block.path:
                    parts.append(f"[Attachment: {block.path}]")
    return "\n".join(parts)
