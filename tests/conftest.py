"""Session-wide test isolation.

A test once resolved the *runtime* data path because a harness forgot to pin
``processing.db_path``: ``build_processing_store`` falls back to ``YEOMAN_HOME`` (or
``~/.yeoman``), so an unpinned store can open live state. This fixture points
``YEOMAN_HOME`` at a throwaway directory for the whole session, which makes that class of
mistake structurally impossible instead of relying on every test author to remember.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True, scope="session")
def _isolated_yeoman_home(tmp_path_factory: pytest.TempPathFactory):
    home = tmp_path_factory.mktemp("yeoman-home")
    previous = os.environ.get("YEOMAN_HOME")
    os.environ["YEOMAN_HOME"] = str(home)
    try:
        yield home
    finally:
        if previous is None:
            os.environ.pop("YEOMAN_HOME", None)
        else:
            os.environ["YEOMAN_HOME"] = previous
