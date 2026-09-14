"""Keep the conformance workflow pointed at the pin the gateway actually enforces.

`a2a conformance` rejects any checkout whose HEAD differs from `CONTRACT_COMMIT`, so the
workflow must not carry its own copy of the commit. A stale copy fails the job with a bare
"contract commit mismatch" and only on CI, so the divergence is asserted here instead.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from yeoman_gateway.a2a.contracts import CONTRACT_COMMIT

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "a2a-conformance.yaml"
CONTRACTS_MODULE = "packages/gateway/yeoman_gateway/a2a/contracts.py"

#: Mirrors the sed program the workflow uses to read the pin out of the gateway.
SED_PROGRAM = 's/^CONTRACT_COMMIT = "\\([0-9a-f]\\{40\\}\\)"$/\\1/p'

RESOLVER_STEP = "Resolve the pinned contract commit"
CONFORMANCE_COMMAND = "uv run yeoman a2a conformance --contracts-checkout .contracts"


def _steps() -> list[dict[str, object]]:
    yaml = pytest.importorskip("yaml")
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return workflow["jobs"]["conformance"]["steps"]


def _step_named(name: str) -> dict[str, object]:
    for step in _steps():
        if step.get("name") == name:
            return step
    raise AssertionError(f"workflow has no step named {name!r}")


def _sed_program(script: str) -> str:
    match = re.search(r"sed -n '([^']+)'", script)
    assert match, f"resolver script has no single-quoted sed program: {script!r}"
    return match.group(1)


def test_contracts_checkout_uses_the_resolved_pin() -> None:
    checkouts = [
        step.get("with", {}) for step in _steps() if step.get("uses") == "actions/checkout@v4"
    ]
    contracts = [with_ for with_ in checkouts if "repository" in with_]
    assert len(contracts) == 1, checkouts
    assert contracts[0]["repository"].endswith("hermes-yeoman-a2a-contracts")
    assert contracts[0]["ref"] == "${{ steps.pin.outputs.ref }}", contracts[0]


def test_workflow_does_not_hardcode_a_contract_sha() -> None:
    hardcoded = re.findall(r"\b[0-9a-f]{40}\b", WORKFLOW.read_text(encoding="utf-8"))
    assert hardcoded == []


def test_resolver_reads_the_pin_from_the_gateway() -> None:
    script = str(_step_named(RESOLVER_STEP)["run"])
    assert CONTRACTS_MODULE in script, script
    assert _sed_program(script) == SED_PROGRAM, script


def test_resolver_sed_program_is_the_one_prototyped_here() -> None:
    result = subprocess.run(
        ["sed", "-n", SED_PROGRAM, str(REPO_ROOT / CONTRACTS_MODULE)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == CONTRACT_COMMIT


def test_conformance_step_still_validates_the_checkout() -> None:
    runs = [str(step["run"]) for step in _steps() if "run" in step]
    assert CONFORMANCE_COMMAND in runs, runs
