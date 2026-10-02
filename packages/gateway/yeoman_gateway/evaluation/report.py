"""Private evaluation reports under var/eval/memory/<UTC stamp>-<kind>/; never reused."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from yeoman_shared.utils.helpers import get_var_path


def new_report_dir(kind: str, *, base: Path | None = None) -> Path:
    root = base if base is not None else get_var_path() / "eval" / "memory"
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    for suffix in range(1000):
        name = f"{stamp}-{kind}" if suffix == 0 else f"{stamp}.{suffix}-{kind}"
        candidate = root / name
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    raise RuntimeError("could not allocate a unique report directory")


def write_report(directory: Path, payload: dict[str, object], markdown: str) -> None:
    (directory / "report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    (directory / "report.md").write_text(markdown, encoding="utf-8")
