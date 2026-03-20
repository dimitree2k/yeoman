"""Run pytest inside the bubblewrap sandbox."""
from __future__ import annotations

from pathlib import Path


def run_tests(*, source_root: Path | None = None, ctx: object) -> dict:
    """Execute pytest in sandbox. Returns {passed, exit_code, output}."""
    root = source_root or ctx.source_dir

    cmd = [
        "python", "-m", "pytest",
        "--tb=short",
        "--basetemp=/tmp/pytest-tmp",
        "-q",
    ]

    sandbox_result = ctx.sandbox.run(
        cmd,
        source_root=root,
        env={
            "PYTEST_CACHE_DIR": "/tmp/pytest-cache",
            "PYTHONPATH": str(root),
        },
    )

    output = sandbox_result["stdout"] + sandbox_result["stderr"]
    return {
        "passed": sandbox_result["exit_code"] == 0,
        "exit_code": sandbox_result["exit_code"],
        "output": output,
    }
