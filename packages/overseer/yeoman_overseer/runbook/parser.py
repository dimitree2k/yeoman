"""Parse runbook Markdown files with YAML frontmatter."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from yeoman_overseer.runbook.schema import RunbookFrontmatter


@dataclass(frozen=True, slots=True)
class Runbook:
    """A parsed runbook — metadata + body + source path."""

    meta: RunbookFrontmatter
    body: str
    path: Path


def parse_runbook(path: Path) -> Runbook:
    """Parse a single runbook file."""
    text = path.read_text(encoding="utf-8")

    if not text.startswith("---"):
        raise ValueError(f"Missing YAML frontmatter in {path}")

    parts = text.split("---", 2)
    if len(parts) < 3:
        raise ValueError(f"Incomplete YAML frontmatter in {path}")

    yaml_text = parts[1]
    body = parts[2].strip()

    try:
        raw = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise ValueError(f"Failed to parse YAML frontmatter in {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError(f"YAML frontmatter must be a mapping in {path}")

    meta = RunbookFrontmatter(**raw)
    return Runbook(meta=meta, body=body, path=path)


def parse_runbook_dir(directory: Path) -> list[Runbook]:
    """Parse all .md files in a directory as runbooks."""
    if not directory.is_dir():
        return []

    runbooks: list[Runbook] = []
    for path in sorted(directory.glob("*.md")):
        runbooks.append(parse_runbook(path))
    return runbooks
