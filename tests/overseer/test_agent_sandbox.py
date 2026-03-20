# tests/overseer/test_agent_sandbox.py
from __future__ import annotations
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from yeoman_overseer.agent.sandbox import Sandbox, SandboxUnavailableError


def test_raises_when_bwrap_not_found():
    with patch("shutil.which", return_value=None):
        Sandbox._bwrap = None
        with pytest.raises(SandboxUnavailableError, match="bwrap not found"):
            Sandbox().run(["echo", "hi"])


def test_run_returns_structured_result(tmp_path):
    mock_result = MagicMock()
    mock_result.stdout = "hello\n"
    mock_result.stderr = ""
    mock_result.returncode = 0

    with patch("shutil.which", return_value="/usr/bin/bwrap"), \
         patch("subprocess.run", return_value=mock_result) as mock_run:
        Sandbox._bwrap = None
        result = Sandbox().run(["echo", "hello"])

    assert result == {"stdout": "hello\n", "stderr": "", "exit_code": 0}


def test_bwrap_args_include_required_mounts(tmp_path):
    mock_result = MagicMock(stdout="", stderr="", returncode=0)
    captured_args = []

    def fake_run(args, **kwargs):
        captured_args.extend(args)
        return mock_result

    with patch("shutil.which", return_value="/usr/bin/bwrap"), \
         patch("subprocess.run", side_effect=fake_run):
        Sandbox._bwrap = None
        Sandbox().run(["true"])

    joined = " ".join(captured_args)
    assert "--unshare-net" in joined
    assert "--unshare-pid" in joined
    assert "--die-with-parent" in joined
    assert "--ro-bind" in joined
    assert "--tmpfs" in joined  # secrets/ masking


def test_sensitive_path_masking_in_args():
    mock_result = MagicMock(stdout="", stderr="", returncode=0)
    captured_args = []

    def fake_run(args, **kwargs):
        captured_args.extend(str(a) for a in args)
        return mock_result

    with patch("shutil.which", return_value="/usr/bin/bwrap"), \
         patch("subprocess.run", side_effect=fake_run):
        Sandbox._bwrap = None
        Sandbox().run(["true"])

    joined = " ".join(captured_args)
    assert "secrets" in joined    # --tmpfs over secrets/
    assert ".env" in joined       # --ro-bind /dev/null over .env


def test_tmpdir_cleaned_up_on_success(tmp_path):
    """After a successful run, the per-call tmpdir must not exist."""
    created_dirs: list[Path] = []

    original_mkdir = Path.mkdir

    def tracking_mkdir(self, *args, **kwargs):
        if "overseer-" in str(self):
            created_dirs.append(self)
        original_mkdir(self, *args, **kwargs)

    mock_result = MagicMock(stdout="", stderr="", returncode=0)
    with patch("shutil.which", return_value="/usr/bin/bwrap"), \
         patch("subprocess.run", return_value=mock_result), \
         patch.object(Path, "mkdir", tracking_mkdir):
        Sandbox._bwrap = None
        Sandbox().run(["true"])

    # All created tmpdirs should be gone
    for d in created_dirs:
        assert not d.exists()
