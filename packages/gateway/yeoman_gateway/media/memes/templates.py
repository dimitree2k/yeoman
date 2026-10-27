"""Vendored meme templates (memegen `config.yml` layout, MIT)."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from yeoman_gateway.media.memes.render import TextBox

ASSETS_DIR = Path(__file__).parent / "assets"
TEMPLATES_DIR = ASSETS_DIR / "templates"


@dataclass(frozen=True, slots=True)
class MemeTemplate:
    """One template image with its caption boxes."""

    id: str
    name: str
    image_path: Path
    boxes: tuple[TextBox, ...]
    example: tuple[str, ...] = ()


def _box(raw: dict[str, object]) -> TextBox:
    def number(key: str, default: float) -> float:
        value = raw.get(key, default)
        return float(value) if isinstance(value, (int, float)) else default

    return TextBox(
        anchor_x=number("anchor_x", 0.0),
        anchor_y=number("anchor_y", 0.0),
        scale_x=number("scale_x", 1.0),
        scale_y=number("scale_y", 0.2),
        angle=number("angle", 0.0),
        align=str(raw.get("align") or "center"),
        upper=str(raw.get("style") or "upper") == "upper",
        color=str(raw.get("color") or "white"),
    )


def _load_one(directory: Path) -> MemeTemplate | None:
    config_path = directory / "config.yml"
    image_path = directory / "default.jpg"
    if not config_path.is_file() or not image_path.is_file():
        return None
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    boxes = tuple(_box(item) for item in raw.get("text") or [] if isinstance(item, dict))
    if not boxes:
        return None
    example = tuple(str(line) for line in raw.get("example") or [] if line)
    return MemeTemplate(
        id=directory.name,
        name=str(raw.get("name") or directory.name),
        image_path=image_path,
        boxes=boxes,
        example=example,
    )


@lru_cache(maxsize=4)
def load_templates(root: Path = TEMPLATES_DIR) -> dict[str, MemeTemplate]:
    """Return every usable template under `root`, keyed by id."""
    templates: dict[str, MemeTemplate] = {}
    for directory in sorted(root.iterdir()) if root.is_dir() else ():
        if directory.is_dir() and (template := _load_one(directory)) is not None:
            templates[template.id] = template
    return templates
