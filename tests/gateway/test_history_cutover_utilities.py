"""Legacy maintenance entrypoints must fence paths before side effects."""

from __future__ import annotations

import importlib
import importlib.util
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
CLI_PATHS = {
    "backfill_memory_insights.py": ("--archive-db",),
    "migrate_person_profile_scope.py": ("--memory-db", "--contacts-db"),
    "backfill_contact_aliases.py": ("--archive-db", "--contacts-db"),
    "estimate_memory_rebuild.py": ("--segments", "--rates", "--output"),
}
PROTECTED = Path("/home/dm/.yeoman/data/ops/cron-jobs.json")


def _load_cli(filename: str, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    path = ROOT / "scripts" / filename
    spec = importlib.util.spec_from_file_location(f"history_cutover_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _arguments(filename: str, root: Path) -> list[str]:
    paths = {
        "--home": root / "home",
        "--archive-db": root / "archive.db",
        "--contacts-db": root / "contacts.db",
        "--memory-db": root / "memory.db",
        "--segments": root / "segments.json",
        "--rates": root / "rates.json",
        "--output": root / "estimate.json",
    }
    args = [part for flag in CLI_PATHS[filename] for part in (flag, str(paths[flag]))]
    if filename == "backfill_memory_insights.py":
        args.append("--dry-run")
    return args


def _set_path_argument(args: list[str], flag: str, path: Path | str) -> list[str]:
    changed = list(args)
    changed[changed.index(flag) + 1] = str(path)
    return changed


def _install_access_sentinels(
    monkeypatch: pytest.MonkeyPatch, module: ModuleType
) -> list[str]:
    touched: list[str] = []

    def touch(name: str) -> Callable[..., object]:
        def fail(*_args: object, **_kwargs: object) -> object:
            touched.append(name)
            raise AssertionError(f"path preflight ran after {name}")

        return fail

    class ScriptPath(type(Path())):
        def open(self, *_args: object, **_kwargs: object) -> object:
            return touch("Path.open")()

        def exists(self) -> bool:
            return touch("Path.exists")()

        def glob(self, *_args: object, **_kwargs: object) -> object:
            return touch("Path.glob")()

        def rglob(self, *_args: object, **_kwargs: object) -> object:
            return touch("Path.rglob")()

        def mkdir(self, *_args: object, **_kwargs: object) -> None:
            touch("Path.mkdir")()

        def read_text(self, *_args: object, **_kwargs: object) -> str:
            return touch("Path.read_text")()

        def write_text(self, *_args: object, **_kwargs: object) -> int:
            return touch("Path.write_text")()

    monkeypatch.setattr(module, "Path", ScriptPath)
    if hasattr(module, "sqlite3"):
        monkeypatch.setattr(
            module,
            "sqlite3",
            SimpleNamespace(connect=touch("sqlite3.connect"), Row=sqlite3.Row),
        )
    if hasattr(module, "load_config"):
        monkeypatch.setattr(module, "load_config", touch("load_config"))
    monkeypatch.setattr(module, "require_isolated_paths", touch("shared require_isolated_paths"))
    for name in ("MemoryService", "ContactsService"):
        if hasattr(module, name):
            monkeypatch.setattr(module, name, touch(name))
    return touched


@pytest.mark.parametrize("filename", CLI_PATHS)
@pytest.mark.parametrize(
    "case", ("defaults", "relative", "runtime", "env-home", "symlink", "sidecar")
)
def test_maintenance_utilities_refuse_runtime_before_access(
    filename: str, case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise each CLI with unsafe paths while trapping every data access route."""
    module = _load_cli(filename, monkeypatch)
    touched = _install_access_sentinels(monkeypatch, module)
    monkeypatch.setattr(sys, "argv", [filename])

    if case == "defaults":
        with pytest.raises(SystemExit):
            module.main()
    else:
        args = _arguments(filename, tmp_path)
        if case == "relative":
            args = _set_path_argument(args, CLI_PATHS[filename][0], "relative.db")
        elif case == "runtime":
            args = _set_path_argument(args, CLI_PATHS[filename][0], PROTECTED)
        elif case == "env-home":
            configured_home = tmp_path / "configured-runtime-home"
            monkeypatch.setenv("YEOMAN_HOME", str(configured_home))
            args = _set_path_argument(
                args, CLI_PATHS[filename][0], configured_home / "data" / "blocked.db"
            )
        elif case == "symlink":
            target = tmp_path / "target.db"
            target.touch()
            link = tmp_path / "input-link.db"
            link.symlink_to(target)
            args = _set_path_argument(args, CLI_PATHS[filename][0], link)
        elif case == "sidecar":
            guarded_path = tmp_path / "sidecar.db"
            sidecar_target = tmp_path / "target-wal"
            sidecar_target.touch()
            Path(f"{guarded_path}-wal").symlink_to(sidecar_target)
            args = _set_path_argument(args, CLI_PATHS[filename][-1], guarded_path)

        monkeypatch.setattr(sys, "argv", [filename, *args])
        with pytest.raises(ValueError, match="isolated|runtime|symlink"):
            module.main()

    assert touched == [], f"unsafe paths reached: {touched}"


def test_isolated_preflight_accepts_paths_and_scripts_import_as_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.syspath_prepend(str(ROOT))
    from scripts.history_maintenance_guard import preflight_isolated_paths

    preflight_isolated_paths(tmp_path / "archive.db", tmp_path / "output.db")
    for filename in CLI_PATHS:
        importlib.import_module(f"scripts.{Path(filename).stem}")


def test_memory_insights_extraction_requires_explicit_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_cli("backfill_memory_insights.py", monkeypatch)
    touched = _install_access_sentinels(monkeypatch, module)
    monkeypatch.setattr(
        sys,
        "argv",
        ["backfill_memory_insights.py", "--archive-db", str(tmp_path / "archive.db")],
    )

    with pytest.raises(SystemExit):
        module.main()

    assert touched == []
