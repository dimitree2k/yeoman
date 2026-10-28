"""Static WhatsApp stickers: 512x512 WebP with transparent padding, under 100 KB."""

from __future__ import annotations

import json
import struct
from io import BytesIO

from PIL import Image

STICKER_SIDE = 512
#: WhatsApp rejects static stickers above 100 KB.
MAX_STICKER_BYTES = 100 * 1024
_QUALITIES = (90, 80, 70, 60, 50, 40, 30)
PACK_NAME = "Arvid Memes"
PACK_PUBLISHER = "Arvid"


def sticker_exif(pack: str = PACK_NAME, publisher: str = PACK_PUBLISHER) -> bytes:
    """EXIF block WhatsApp reads the sticker pack name and author from.

    A little-endian TIFF header with one IFD entry, tag 0x5741, holding a JSON document;
    the same layout WhatsApp's own sticker makers write.
    """
    payload = json.dumps(
        {
            "sticker-pack-id": "yeoman-memes",
            "sticker-pack-name": pack,
            "sticker-pack-publisher": publisher,
            "emojis": ["😂"],
        },
        ensure_ascii=False,
    ).encode("utf-8")
    header = b"II*\x00" + struct.pack("<I", 8)
    # One entry: tag 0x5741, type 7 (UNDEFINED), count, offset of the data (8 + 2 + 12 + 4).
    entry = struct.pack("<HHII", 0x5741, 7, len(payload), 26)
    return header + struct.pack("<H", 1) + entry + struct.pack("<I", 0) + payload


def to_sticker(image: Image.Image) -> bytes:
    """Fit `image` into a transparent 512x512 square and encode it as a small WebP."""
    source = image.convert("RGBA")
    source.thumbnail((STICKER_SIDE, STICKER_SIDE), Image.LANCZOS)
    canvas = Image.new("RGBA", (STICKER_SIDE, STICKER_SIDE), (0, 0, 0, 0))
    canvas.paste(
        source,
        ((STICKER_SIDE - source.width) // 2, (STICKER_SIDE - source.height) // 2),
        source,
    )
    exif = sticker_exif()
    data = b""
    for quality in _QUALITIES:
        buffer = BytesIO()
        canvas.save(buffer, "WEBP", quality=quality, method=6, exif=exif)
        data = buffer.getvalue()
        if len(data) <= MAX_STICKER_BYTES:
            return data
    # A 512x512 picture at quality 30 is far below the limit in practice.
    raise ValueError(f"sticker is still {len(data)} bytes at the lowest quality")
