"""Deploy pipeline utilities."""

from __future__ import annotations

import hashlib
from pathlib import Path


def hash_bridge_sources(src_dir: Path) -> str:
    """SHA-256 hash of .ts source files (excluding tests and declarations)."""
    h = hashlib.sha256()
    for ts_file in sorted(src_dir.glob("*.ts")):
        if ts_file.name.endswith((".test.ts", ".spec.ts", ".d.ts")):
            continue
        h.update(ts_file.name.encode())
        h.update(ts_file.read_bytes())
    return h.hexdigest()


def bridge_is_stale(src_dir: Path, dist_dir: Path) -> bool:
    """Check whether bridge dist is stale relative to source."""
    if not dist_dir.exists():
        return True
    hash_file = dist_dir / ".build-hash"
    if not hash_file.exists():
        return True
    stored = hash_file.read_text().strip()
    current = hash_bridge_sources(src_dir)
    return stored != current
