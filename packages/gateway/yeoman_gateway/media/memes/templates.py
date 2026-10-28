"""Vendored meme templates (memegen `config.yml` layout, MIT)."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from yeoman_gateway.media.memes.render import TextBox

ASSETS_DIR = Path(__file__).parent / "assets"
TEMPLATES_DIR = ASSETS_DIR / "templates"

#: The joke each template tells, caption box by caption box. The model picks a template
#: by the shape of its joke, so a name alone ("Drakeposting") is not enough.
TEMPLATE_JOKES: dict[str, str] = {
    "aag": '1 something unexplained, 2 one absurd explanation for everything ("Aliens.")',
    "afraid": '1 something everyone seems to know, 2 "and at this point I\'m too afraid to ask"',
    "ants": '1 "do you want X?", 2 "because that\'s how you get X": a habit and its '
    "predictable result",
    "astronaut": '1 a late, shocked realization "Wait, it\'s X?", 2 "Always has been", '
    "3 who just noticed, 4 who always knew",
    "bihw": '1 "it ain\'t much", 2 "but it\'s honest work": proud of a tiny result',
    "both": '1 a choice between two things, 2 "why not both?"',
    "buzz": '1 X, 2 "X everywhere": something suddenly everywhere',
    "cb": "1 the setup of a confession, 2 the embarrassing twist",
    "cmm": "1 one provocative hot take, defended at a table",
    "db": "1 the tempting new thing, 2 who is tempted, 3 what they already have and neglect",
    "doge": '1-2 fake-naive praise in broken words ("such X", "very Y")',
    "drake": "1 what gets rejected, 2 what gets preferred instead",
    "exit": "1 the sensible road ahead, 2 the tempting exit, 3 who swerves to the exit",
    "fine": '1 the disaster going on, 2 "this is fine": calm denial',
    "fwp": "1-2 a complaint about a trivial luxury problem",
    "gb": '1-4 four ideas, each more absurdly "enlightened" than the last',
    "gru": "1 step of a plan, 2 next step, 3 the step that backfires, 4 the same as 3",
    "harold": "1 the situation, 2 the pain hidden behind a forced smile",
    "iw": "1 a reckless action, 2 an even worse escalation",
    "kermit": "1 a passive-aggressive observation about someone, "
    '2 "but that\'s none of my business"',
    "mordor": '1 "one does not simply", 2 a task that looks easy and is not',
    "morpheus": '1 "what if I told you", 2 an obvious truth revealed as if profound',
    "pigeon": '1 who is confused, 2 the thing, 3 the wrong label "Is this X?"',
    "pooh": "1 the plain version, 2 the same thing said pretentiously",
    "rollsafe": '1 a problem, 2 the "smart" loophole that is obviously flawed',
    "spongebob": "1 something said, 2 the same words repeated in mOcKiNg case",
    "stonks": '1 a dumb money move, 2 "stonks": celebrated anyway',
    "success": "1 a setup that should go wrong, 2 the small unexpected win",
    "tried": '1 an obvious failure, 2 "at least you tried"',
    "woman-cat": "1 an angry accusation, 2 the unbothered, absurd reply",
    "wonka": '1 "oh, you X?", 2 a condescending conclusion "you must Y"',
    "yuno": '1 who or what, 2 "Y U NO" + the thing they keep not doing',
}


@dataclass(frozen=True, slots=True)
class MemeTemplate:
    """One template image with its caption boxes."""

    id: str
    name: str
    image_path: Path
    boxes: tuple[TextBox, ...]
    example: tuple[str, ...] = ()
    joke: str = ""


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
        joke=TEMPLATE_JOKES.get(directory.name, ""),
    )


@lru_cache(maxsize=4)
def load_templates(root: Path = TEMPLATES_DIR) -> dict[str, MemeTemplate]:
    """Return every usable template under `root`, keyed by id."""
    templates: dict[str, MemeTemplate] = {}
    for directory in sorted(root.iterdir()) if root.is_dir() else ():
        if directory.is_dir() and (template := _load_one(directory)) is not None:
            templates[template.id] = template
    return templates
