"""Run the pinned contracts checkout's A2A conformance suite."""

from __future__ import annotations

import subprocess
import sys
from importlib import resources
from pathlib import Path
from typing import Any

import a2a_contracts

from yeoman_gateway.a2a.contracts import CONTRACT_COMMIT, CONTRACT_RELEASE, PROFILE_URI


class ConformanceError(RuntimeError):
    """The supplied contract checkout cannot prove conformance."""


def _files(root: Any) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*.schema.json")
    }


def _commit(checkout: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise ConformanceError(f"cannot read contract checkout commit: {result.stderr.strip()}")
    return result.stdout.strip()


def run(checkout: Path) -> tuple[int, int]:
    """Check the exact checkout, installed schema package, and all contract fixtures."""
    if not checkout.is_dir():
        raise ConformanceError(f"contracts checkout does not exist: {checkout}")
    if _commit(checkout) != CONTRACT_COMMIT:
        raise ConformanceError(f"contract commit mismatch: expected {CONTRACT_COMMIT}")
    if a2a_contracts.CONTRACT_VERSION != CONTRACT_RELEASE:
        raise ConformanceError("contract release mismatch")
    if a2a_contracts.PROFILE_URI != PROFILE_URI:
        raise ConformanceError("contract profile mismatch")

    source_schemas = checkout / "schemas"
    installed_schemas = resources.files("a2a_contracts").joinpath("schemas")
    if _files(source_schemas) != _files(installed_schemas):
        raise ConformanceError("contract schema drift between checkout and installed package")

    positive = sorted((checkout / "conformance" / "positive").glob("*.json"))
    negative = sorted((checkout / "conformance" / "negative").glob("*.json"))
    if not positive or not negative:
        raise ConformanceError("contract checkout must provide positive and negative fixtures")

    result = subprocess.run(
        [sys.executable, "scripts/validate_contract.py"],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise ConformanceError(result.stderr.strip() or result.stdout.strip())
    return len(positive), len(negative)


def main(checkout: Path) -> int:
    positive, negative = run(checkout)
    print(f"A2A contract conformance: {positive} positive, {negative} negative fixtures")
    return 0
