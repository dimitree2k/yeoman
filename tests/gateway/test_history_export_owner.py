"""Owner reads through `yeoman history read` must match the owner on that channel."""
from __future__ import annotations

from dataclasses import dataclass

from yeoman_gateway.history.export import _policy_owner
from yeoman_gateway.knowledge.models import TrustedReadContext
from yeoman_gateway.knowledge.runtime import RuntimeKnowledgePolicy

PHONE = "whatsapp:491700000001"
LID = "whatsapp:123456789@lid"


@dataclass
class _Engine:
    owners: dict


def _policy(*principals: str) -> RuntimeKnowledgePolicy:
    return RuntimeKnowledgePolicy(engine=_Engine(owners={}), admin_principals=frozenset(principals))


def _context(actor: str, recipients: frozenset[str], *, owner: bool = True) -> TrustedReadContext:
    return TrustedReadContext(
        principal_id=actor, channel="whatsapp", chat_id="120363000000000000@g.us",
        recipient_principals=recipients, membership_revision="owner", policy_revision=1,
        purpose="admin", now_ms=1, is_direct=True, owner=owner,
    )


def test_owner_with_telegram_principal_reads_whatsapp() -> None:
    # admin_actor() picks the Telegram form first; the WhatsApp actor must still count.
    policy = _policy("telegram:4711", PHONE, LID)
    assert policy.admin_actor() == "telegram:4711"
    assert _policy_owner(policy, _context(PHONE, frozenset({PHONE, LID})))


def test_actor_outside_the_owner_set_is_refused() -> None:
    policy = _policy("telegram:4711", PHONE, LID)
    assert not _policy_owner(policy, _context("whatsapp:499999", frozenset({PHONE, LID})))
    assert not _policy_owner(policy, _context(PHONE, frozenset({PHONE}), owner=False))


def test_recipients_without_a_declared_form_are_refused() -> None:
    policy = _policy("telegram:4711", PHONE)
    other = "whatsapp:499999"
    assert not _policy_owner(policy, _context(other, frozenset({other})))
