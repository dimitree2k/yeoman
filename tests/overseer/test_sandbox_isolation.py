from __future__ import annotations
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
from yeoman_overseer.agent.sandbox import Sandbox


def test_each_sandbox_run_gets_unique_tmpdir():
    created: list[str] = []
    original_mkdir = Path.mkdir

    def tracking_mkdir(self, *args, **kwargs):
        if "overseer-" in str(self):
            created.append(str(self))
        original_mkdir(self, *args, **kwargs)

    mock_result = MagicMock(stdout="", stderr="", returncode=0)

    with patch("shutil.which", return_value="/usr/bin/bwrap"), \
         patch("subprocess.run", return_value=mock_result), \
         patch.object(Path, "mkdir", tracking_mkdir):
        Sandbox._bwrap = None
        sb = Sandbox()
        sb.run(["echo", "a"])
        sb.run(["echo", "b"])

    assert len(created) == 2
    assert created[0] != created[1], "Two sandbox calls must use distinct tmpdirs"


def test_tmpdir_names_contain_hex_uuid():
    created: list[str] = []
    original_mkdir = Path.mkdir

    def tracking_mkdir(self, *args, **kwargs):
        if "overseer-" in str(self):
            created.append(Path(self).name)
        original_mkdir(self, *args, **kwargs)

    mock_result = MagicMock(stdout="", stderr="", returncode=0)

    with patch("shutil.which", return_value="/usr/bin/bwrap"), \
         patch("subprocess.run", return_value=mock_result), \
         patch.object(Path, "mkdir", tracking_mkdir):
        Sandbox._bwrap = None
        Sandbox().run(["true"])

    assert len(created) == 1
    name = created[0]
    prefix, _, hex_part = name.partition("-")
    assert prefix == "overseer"
    assert len(hex_part) == 32
    int(hex_part, 16)  # Must be valid hex
