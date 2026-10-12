"""Identity observation issuer rejects caller-upgraded evidence."""

from __future__ import annotations

from dataclasses import replace

import pytest
from yeoman_gateway.knowledge.authority import FakeSourceAuthority
from yeoman_gateway.knowledge.models import Identifier, KnowledgeError, TrustedIdentityObservation


def _unverified_pair() -> TrustedIdentityObservation:
    phone = Identifier("whatsapp", "phone_jid", "491700000005@s.whatsapp.net", "account-a")
    lid = Identifier("whatsapp", "lid", "100000005@lid", "account-a")
    return TrustedIdentityObservation(
        identifiers=(phone, lid),
        evidence_ref="message:unverified",
        observed_at_ms=1_700_000_006_000,
        mapping_verified=False,
        account_namespace="account-a",
    )


def test_fake_issuer_rejects_caller_upgraded_mapping_flag() -> None:
    authority = FakeSourceAuthority()
    issued = _unverified_pair()
    authority.issue_observation(issued)

    with pytest.raises(KnowledgeError, match="does not match its evidence"):
        authority.verify_observation(replace(issued, mapping_verified=True))


def test_runtime_issuer_rejects_caller_upgraded_mapping_flag() -> None:
    from yeoman_gateway.knowledge.runtime import RuntimeKnowledgeSources

    issued = _unverified_pair()
    sources = RuntimeKnowledgeSources()
    sources.observe(issued)

    with pytest.raises(KnowledgeError, match="does not match its evidence"):
        sources.verify_observation(replace(issued, mapping_verified=True))
