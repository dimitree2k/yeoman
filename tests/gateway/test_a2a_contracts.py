from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from yeoman_gateway.a2a.contracts import (
    CONTRACT_COMMIT,
    CONTRACT_RELEASE,
    PROFILE_URI,
    A2AContractValidationError,
    ContractSchemas,
)


def test_installed_contract_metadata_is_pinned() -> None:
    import a2a_contracts

    assert CONTRACT_RELEASE == a2a_contracts.CONTRACT_VERSION == "1.0.0"
    assert CONTRACT_COMMIT == "e408cc3d10cc9c76a245d851d7abfa2027874d51"
    assert PROFILE_URI == a2a_contracts.PROFILE_URI == "urn:hermes-yeoman:a2a-profile:v1"


def test_import_rejects_mismatched_installed_contract() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import a2a_contracts; a2a_contracts.CONTRACT_VERSION='9.9.9'; import yeoman_gateway.a2a.contracts",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "contract release mismatch" in result.stderr


def test_import_rejects_mismatched_installed_profile_uri() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import a2a_contracts; a2a_contracts.PROFILE_URI='urn:wrong'; import yeoman_gateway.a2a.contracts",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "contract profile mismatch" in result.stderr


def test_registry_resolves_refs_and_rejects_unknown_fields() -> None:
    schemas = ContractSchemas.load()
    schemas.validate_result(
        {
            "skill": "conversation",
            "status": "failed",
            "error": {"code": "NOPE", "message": "no", "retryable": False},
            "correlation": {"task_id": "task-1"},
        }
    )
    with pytest.raises(A2AContractValidationError, match=r"\$: additional"):
        schemas.validate_request("conversation", {"text": "hello", "extra": True})


def test_registry_rejects_unknown_skill() -> None:
    with pytest.raises(A2AContractValidationError, match="unknown skill"):
        ContractSchemas.load().validate_request("unknown.skill", {})


def test_registry_validates_invocation_and_skill_response() -> None:
    schemas = ContractSchemas.load()
    schemas.validate_invocation({"skill": "conversation", "input": {"text": "hello"}})
    schemas.validate_response("conversation", {"text": "hi"})
    with pytest.raises(A2AContractValidationError, match=r"\$: required"):
        schemas.validate_response("conversation", {})


def test_local_contract_override_keeps_project_metadata_untouched() -> None:
    script = Path("scripts/use-local-a2a-contracts")
    pyproject = Path("pyproject.toml")
    before = pyproject.read_bytes()
    result = subprocess.run(
        [str(script), "/tmp/hermes-yeoman-a2a-contracts-v1.0.0", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert pyproject.read_bytes() == before
    assert "uv" in result.stdout.lower() or "uv" in result.stderr.lower()
