from __future__ import annotations

import os
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

    assert CONTRACT_RELEASE == a2a_contracts.CONTRACT_VERSION == "1.0.1"
    assert CONTRACT_COMMIT == "b8886616664b922538a91c7f78c608c963d0826c"
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


def test_local_contract_override_keeps_project_metadata_untouched(tmp_path: Path) -> None:
    script = Path("scripts/use-local-a2a-contracts")
    pyproject = Path("pyproject.toml")
    checkout = tmp_path / "contracts"
    checkout.mkdir()
    before = pyproject.read_bytes()
    result = subprocess.run(
        [str(script), str(checkout), "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert pyproject.read_bytes() == before
    assert "uv" in result.stdout.lower() or "uv" in result.stderr.lower()


def test_local_contract_override_run_imports_supplied_checkout(tmp_path: Path) -> None:
    checkout = tmp_path / "contracts"
    package = checkout / "a2a_contracts"
    package.mkdir(parents=True)
    (checkout / "pyproject.toml").write_text(
        """
[build-system]
requires = []
build-backend = "backend"
backend-path = ["."]

[project]
name = "hermes-yeoman-a2a-contracts"
version = "1.0.1"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (checkout / "backend.py").write_text(
        """
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

NAME = "hermes_yeoman_a2a_contracts"
DIST_INFO = f"{NAME}-1.0.1.dist-info"
WHEEL = f"{NAME}-1.0.1-py3-none-any.whl"


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    path = Path(wheel_directory) / WHEEL
    with ZipFile(path, "w", ZIP_DEFLATED) as wheel:
        wheel.writestr("a2a_contracts/__init__.py", "MARKER = 'local-checkout'\\n")
        wheel.writestr(
            f"{DIST_INFO}/METADATA",
            "Metadata-Version: 2.1\\nName: hermes-yeoman-a2a-contracts\\nVersion: 1.0.1\\n",
        )
        wheel.writestr(
            f"{DIST_INFO}/WHEEL",
            "Wheel-Version: 1.0\\nGenerator: contract-test\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n",
        )
        wheel.writestr(f"{DIST_INFO}/RECORD", "")
    return WHEEL


def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None):
    path = Path(metadata_directory) / DIST_INFO
    path.mkdir()
    (path / "METADATA").write_text(
        "Metadata-Version: 2.1\\nName: hermes-yeoman-a2a-contracts\\nVersion: 1.0.1\\n"
    )
    (path / "WHEEL").write_text(
        "Wheel-Version: 1.0\\nGenerator: contract-test\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n"
    )
    return DIST_INFO


def get_requires_for_build_wheel(config_settings=None):
    return []
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (package / "__init__.py").write_text("MARKER = 'source-not-imported'\n", encoding="utf-8")

    result = subprocess.run(
        [
            str(Path("scripts/use-local-a2a-contracts")),
            str(checkout),
            "run",
            "--isolated",
            "--no-project",
            "--offline",
            "--no-cache",
            "python",
            "-c",
            "import a2a_contracts; print(a2a_contracts.MARKER)",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "local-checkout"


def test_validation_error_names_the_missing_required_field() -> None:
    with pytest.raises(A2AContractValidationError) as caught:
        ContractSchemas.load().validate_request(
            "research.deep", {"question": "Analyse AAPL", "output_format": "markdown"}
        )
    message = str(caught.value)
    assert "idempotency_key" in message
    assert "required" in message
    assert "$" in message


def test_validation_error_names_the_unexpected_field() -> None:
    with pytest.raises(A2AContractValidationError) as caught:
        ContractSchemas.load().validate_request(
            "research.deep", {"question": "Analyse AAPL", "ticker": "AAPL"}
        )
    message = str(caught.value)
    assert "ticker" in message
    assert "additionalProperties" in message


def test_validation_error_reports_every_root_level_violation() -> None:
    """The 2026-09-14 production failure: a missing key and two unknown fields at once."""
    with pytest.raises(A2AContractValidationError) as caught:
        ContractSchemas.load().validate_request(
            "research.deep",
            {"question": "Analyse AAPL", "topic": "AAPL stock analysis", "ticker": "AAPL"},
        )
    message = str(caught.value)
    for fragment in ("idempotency_key", "ticker", "topic", "additionalProperties", "required"):
        assert fragment in message, message


def test_validation_error_names_a_nested_offending_field() -> None:
    with pytest.raises(A2AContractValidationError) as caught:
        ContractSchemas.load().validate_invocation(
            {"skill": "research.deep", "input": {"question": "ok", "idempotency_key": "k"}, "extra": 1}
        )
    message = str(caught.value)
    assert "extra" in message
    assert "additionalProperties" in message


def test_validation_error_is_bounded_and_single_line() -> None:
    payload = {f"unexpected_{index}": index for index in range(200)}
    with pytest.raises(A2AContractValidationError) as caught:
        ContractSchemas.load().validate_invocation(
            {"skill": "research.deep", "input": {"question": "ok", "idempotency_key": "k"}, **payload}
        )
    message = str(caught.value)
    assert "\n" not in message
    assert len(message) <= 500


def test_validation_error_names_only_the_fields_the_payload_omits() -> None:
    """jsonschema lists every required key; the message must name the absent one only."""
    with pytest.raises(A2AContractValidationError) as caught:
        ContractSchemas.load().validate_request(
            "research.deep", {"question": "Analyse AAPL", "output_format": "markdown"}
        )
    message = str(caught.value)
    assert "idempotency_key" in message
    missing_clause = message.split("missing required fields:")[1].split(")")[0]
    assert "question" not in missing_clause, message


def test_validation_error_does_not_repeat_the_validator_message() -> None:
    with pytest.raises(A2AContractValidationError) as caught:
        ContractSchemas.load().validate_request(
            "research.deep", {"question": "Analyse AAPL", "ticker": "AAPL"}
        )
    message = str(caught.value)
    assert message.count("ticker") == 1, message
    assert "were unexpected" not in message, message
