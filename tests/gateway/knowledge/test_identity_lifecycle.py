"""Focused lifecycle regressions for verified identity and alias maintenance."""

from yeoman_gateway.knowledge.models import Identifier, IdentifierBinding


def test_ended_binding_with_unknown_end_does_not_cover_any_instant():
    binding = IdentifierBinding(
        person_id="synthetic-person",
        identifier=Identifier("whatsapp", "phone_jid", "49180000040@s.whatsapp.net"),
        evidence_ref="synthetic-evidence",
        status="ended",
        valid_from_ms=1_000,
        valid_until_ms=0,
    )

    assert not binding.covers(1_000)


