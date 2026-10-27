"""Draw outlined meme captions onto an image with Pillow."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageColor, ImageDraw, ImageFont

FONT_PATH = Path(__file__).parent / "assets" / "fonts" / "Oswald-Variable.ttf"
_FONT_WEIGHT = 700
#: Glyphs that reach highest (accents, breve) and lowest (descenders) in Latin and Cyrillic.
_TALL_GLYPHS = "ÁÜЙЁ"
_DEEP_GLYPHS = "gjpqyДЦЩу"
_MIN_FONT_SIZE = 10
#: Long side of every rendered meme; WhatsApp recompresses larger photos anyway.
MAX_OUTPUT_SIDE = 1024


@dataclass(frozen=True, slots=True)
class TextBox:
    """Caption area as fractions of the image, following memegen's `text:` entries."""

    anchor_x: float = 0.0
    anchor_y: float = 0.0
    scale_x: float = 1.0
    scale_y: float = 0.2
    angle: float = 0.0
    align: str = "center"
    upper: bool = True
    color: str = "white"


#: Classic top/bottom layout for photos and generated backgrounds.
TOP_BOTTOM: tuple[TextBox, TextBox] = (
    TextBox(anchor_y=0.0),
    TextBox(anchor_y=0.8),
)


@lru_cache(maxsize=64)
def _font(size: int) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(str(FONT_PATH), size)
    try:
        font.set_variation_by_axes([_FONT_WEIGHT])
    except OSError, ValueError:
        pass  # FreeType without variation support renders the default instance
    return font


def _line_extent(font: ImageFont.FreeTypeFont) -> tuple[int, int]:
    """(offset of the highest ink below the draw origin, height of one line of ink)."""
    top = int(font.getbbox(_TALL_GLYPHS)[1])
    bottom = int(font.getbbox(_DEEP_GLYPHS)[3])
    return top, bottom - top


def _outline_color(fill: str) -> str:
    try:
        rgb = ImageColor.getrgb(fill)
    except ValueError:
        return "black"
    luminance = 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]
    return "white" if luminance < 110 else "black"


def _wrap(text: str, font: ImageFont.FreeTypeFont, max_width: float) -> list[str] | None:
    """Greedy word wrap; None when a single word is wider than the box."""
    lines: list[str] = []
    for paragraph in text.splitlines() or [""]:
        current = ""
        for word in paragraph.split():
            candidate = f"{current} {word}".strip()
            if font.getlength(candidate) <= max_width:
                current = candidate
                continue
            if not current or font.getlength(word) > max_width:
                return None
            lines.append(current)
            current = word
        lines.append(current)
    return lines


def _fit(text: str, width: float, height: float) -> tuple[ImageFont.FreeTypeFont, list[str]] | None:
    """Largest font size whose wrapped lines fit inside width x height."""
    best: tuple[ImageFont.FreeTypeFont, list[str]] | None = None
    low, high = _MIN_FONT_SIZE, max(_MIN_FONT_SIZE, int(height))
    while low <= high:
        size = (low + high) // 2
        font = _font(size)
        lines = _wrap(text, font, width)
        if lines is not None and len(lines) * _line_extent(font)[1] <= height:
            best = (font, lines)
            low = size + 1
        else:
            high = size - 1
    return best


def _draw_box(image: Image.Image, text: str, box: TextBox) -> None:
    text = " ".join(text.split()) if "\n" not in text else text.strip()
    if not text:
        return
    if box.upper:
        text = text.upper()
    img_w, img_h = image.size
    box_w = max(1, round(box.scale_x * img_w))
    box_h = max(1, round(box.scale_y * img_h))
    pad = max(2, round(min(box_w, box_h) * 0.04))
    pad_x = max(pad, round(box_w * 0.03))
    fitted = _fit(text, box_w - 2 * pad_x, box_h - 2 * pad)
    if fitted is None:
        # Text too long for the box even at the minimum size: shrink, never drop it.
        font = _font(_MIN_FONT_SIZE)
        lines = _wrap(text, font, box_w - 2 * pad_x) or [text]
    else:
        font, lines = fitted
    size = int(font.size)
    stroke = max(1, round(size / 14))

    layer = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    top_offset, line_h = _line_extent(font)
    y = (box_h - line_h * len(lines)) / 2 - top_offset
    for line in lines:
        line_w = font.getlength(line)
        if box.align == "left":
            x = pad_x
        elif box.align == "right":
            x = box_w - pad_x - line_w
        else:
            x = (box_w - line_w) / 2
        draw.text(
            (x, y),
            line,
            font=font,
            fill=box.color,
            stroke_width=stroke,
            stroke_fill=_outline_color(box.color),
        )
        y += line_h

    left = round(box.anchor_x * img_w)
    top = round(box.anchor_y * img_h)
    if box.angle:
        # Same convention as memegen: positive angles turn counter-clockwise.
        rotated = layer.rotate(box.angle, resample=Image.BICUBIC, expand=True)
        left += (box_w - rotated.width) // 2
        top += (box_h - rotated.height) // 2
        layer = rotated
    # paste (unlike alpha_composite) accepts offsets a rotated box pushes past the edge.
    image.paste(layer, (left, top), layer)


def render_meme(
    base: Image.Image,
    captions: list[str] | tuple[str, ...],
    boxes: tuple[TextBox, ...] = TOP_BOTTOM,
) -> Image.Image:
    """Return an RGB copy of `base` with one caption per box (missing ones stay empty)."""
    image = base.convert("RGBA")
    longest = max(image.size)
    if longest > MAX_OUTPUT_SIDE:
        factor = MAX_OUTPUT_SIDE / longest
        image = image.resize(
            (max(1, round(image.width * factor)), max(1, round(image.height * factor))),
            Image.LANCZOS,
        )
    for text, box in zip(captions, boxes):
        _draw_box(image, str(text or ""), box)
    return image.convert("RGB")
