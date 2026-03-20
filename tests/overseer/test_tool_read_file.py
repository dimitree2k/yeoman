from pathlib import Path
from unittest.mock import MagicMock

from yeoman_overseer.agent.tools.read_file import execute


def _ctx(home: Path, source: Path):
    ctx = MagicMock()
    ctx.yeoman_home = home
    ctx.source_dir = source
    return ctx


def test_read_allowed_file(tmp_path):
    home = tmp_path / ".yeoman"
    home.mkdir()
    (home / "config.json").write_text('{"x": 1}')
    ctx = _ctx(home, tmp_path / "source")
    result = execute({"path": str(home / "config.json")}, ctx)
    assert '{"x": 1}' in result


def test_read_blocks_dot_env(tmp_path):
    home = tmp_path / ".yeoman"
    home.mkdir()
    (home / ".env").write_text("SECRET=abc")
    ctx = _ctx(home, tmp_path / "source")
    result = execute({"path": str(home / ".env")}, ctx)
    assert "blocked" in result.lower() or "denied" in result.lower()


def test_read_blocks_secrets_dir(tmp_path):
    home = tmp_path / ".yeoman"
    secrets = home / "secrets"
    secrets.mkdir(parents=True)
    (secrets / "creds.json").write_text("{}")
    ctx = _ctx(home, tmp_path / "source")
    result = execute({"path": str(secrets / "creds.json")}, ctx)
    assert "blocked" in result.lower() or "denied" in result.lower()


def test_read_blocks_git_dir(tmp_path):
    home = tmp_path / ".yeoman"
    git_dir = home / ".git" / "hooks"
    git_dir.mkdir(parents=True)
    (git_dir / "post-commit").write_text("#!/bin/bash\necho pwned")
    ctx = _ctx(home, tmp_path / "source")
    result = execute({"path": str(git_dir / "post-commit")}, ctx)
    assert "blocked" in result.lower() or "denied" in result.lower()


def test_read_blocks_path_outside_roots(tmp_path):
    home = tmp_path / ".yeoman"
    home.mkdir()
    ctx = _ctx(home, tmp_path / "source")
    result = execute({"path": "/etc/passwd"}, ctx)
    assert "blocked" in result.lower() or "denied" in result.lower()


def test_read_missing_file_returns_error(tmp_path):
    home = tmp_path / ".yeoman"
    home.mkdir()
    ctx = _ctx(home, tmp_path / "source")
    result = execute({"path": str(home / "nonexistent.txt")}, ctx)
    assert "not found" in result.lower() or "error" in result.lower()
