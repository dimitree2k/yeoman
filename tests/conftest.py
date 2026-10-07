"""Session-wide test isolation.

A test once resolved the *runtime* data path because a harness forgot to pin
``processing.db_path``: ``build_processing_store`` falls back to ``YEOMAN_HOME`` (or
``~/.yeoman``), so an unpinned store can open live state. This fixture points
``YEOMAN_HOME`` at a throwaway directory for the whole session, which makes that class of
mistake structurally impossible instead of relying on every test author to remember.

Some config defaults are literal ``~/.yeoman/...`` paths (memory and knowledge databases,
media folders) and ignore ``YEOMAN_HOME``; in 2026-10 a default ``Config`` in a test opened
the live ``memory.db``. The fixture therefore also points ``HOME`` at a throwaway directory,
and an audit hook fails any test that still opens, connects to, removes or renames a path
inside the real runtime directory.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

_RUNTIME = os.path.realpath(os.path.expanduser("~/.yeoman"))
_GUARDED_EVENTS = {"open", "sqlite3.connect", "os.remove", "os.rename", "os.rmdir", "os.mkdir", "shutil.rmtree"}


def _event_paths(event: str, args: tuple) -> list[object]:
    if event == "os.rename":
        return list(args[:2])
    return list(args[:1])


def _inside_runtime(raw: object) -> bool:
    if isinstance(raw, bytes):
        raw = os.fsdecode(raw)
    if isinstance(raw, os.PathLike):
        raw = os.fspath(raw)
    if not isinstance(raw, str) or raw in ("", ":memory:"):
        return False
    if raw.startswith("file:"):
        raw = raw[len("file:"):].split("?", 1)[0]
    path = os.path.realpath(os.path.expanduser(raw))
    return path == _RUNTIME or path.startswith(_RUNTIME + os.sep)


def _guard_live_runtime(event: str, args: tuple) -> None:
    if event not in _GUARDED_EVENTS:
        return
    for raw in _event_paths(event, args):
        if _inside_runtime(raw):
            raise RuntimeError(f"test touched the live Yeoman runtime ({event}): {raw!r}")


sys.addaudithook(_guard_live_runtime)


_PREVIOUS_ENV: dict[str, str | None] = {}


_SESSION_ROOT: list[Path] = []


def pytest_configure(config: pytest.Config) -> None:
    # Runs before collection: some modules resolve runtime paths at import time.
    parent = Path(os.path.expanduser("~/.cache/yeoman-tests/session-homes"))
    parent.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="session-", dir=parent))
    _SESSION_ROOT.append(root)
    for name, value in (("YEOMAN_HOME", root / "yeoman-home"), ("HOME", root / "user-home")):
        value.mkdir()
        _PREVIOUS_ENV[name] = os.environ.get(name)
        os.environ[name] = str(value)


def pytest_unconfigure(config: pytest.Config) -> None:
    for name, value in _PREVIOUS_ENV.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    for root in _SESSION_ROOT:
        shutil.rmtree(root, ignore_errors=True)
