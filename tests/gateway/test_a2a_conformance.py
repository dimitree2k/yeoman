from pathlib import Path

import pytest
from yeoman_gateway.a2a import conformance

CONTRACTS_CHECKOUT = Path("/tmp/hermes-yeoman-a2a-contracts-v1.0.1")


def test_conformance_runs_every_pinned_contract_fixture() -> None:
    if not CONTRACTS_CHECKOUT.is_dir():
        pytest.skip(f"contracts checkout unavailable: {CONTRACTS_CHECKOUT}")

    assert conformance.run(CONTRACTS_CHECKOUT) == (9, 6)


def test_conformance_requires_a_contract_checkout(tmp_path: Path) -> None:
    with pytest.raises(conformance.ConformanceError, match="checkout does not exist"):
        conformance.run(tmp_path / "missing")
