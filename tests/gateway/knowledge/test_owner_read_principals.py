"""Owner Knowledge reads must be identifier-complete.

A policy may declare its owner principals explicitly (``admin_principals``).  Knowledge
expands each declared principal through the stored identifier bindings of the owner
person, so one read context carries every proven form of that human - LID, phone and
the bare forms the archive happens to store.  Expansion is proof-based: an identity that
no binding proves contributes only its declared form.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from yeoman_gateway.knowledge.models import TrustedReadContext
from yeoman_gateway.knowledge.runtime import RuntimeKnowledgePolicy, select_owner_principal
from yeoman_gateway.policy.schema import PolicyConfig

PHONE_VALUE = "4910000000777"
PHONE = f"whatsapp:{PHONE_VALUE}"
LID_VALUE = "34596062240001@lid"
LID = f"whatsapp:{LID_VALUE}"
PHONE_CHAT = f"{PHONE_VALUE}@s.whatsapp.net"
STRANGER = "whatsapp:4910000000099"
GROUP_CHAT = "120363000000000001@g.us"
UNPROVEN = "whatsapp:4910000008888"


# ── policy model ─────────────────────────────────────────────────────────────


def test_policy_accepts_and_round_trips_admin_principals() -> None:
    declared = [PHONE, "telegram:100000001"]
    policy = PolicyConfig.model_validate({"admin_principals": declared})

    assert policy.admin_principals == declared
    dumped = policy.model_dump(by_alias=True, exclude_none=True)
    assert dumped["admin_principals"] == declared
    assert PolicyConfig.model_validate(dumped).admin_principals == declared


def test_policy_without_admin_principals_keeps_the_owners_fallback() -> None:
    policy = PolicyConfig.model_validate({"owners": {"whatsapp": [PHONE_VALUE]}})

    assert policy.admin_principals == []
    assert policy.owners["whatsapp"] == [PHONE_VALUE]
    # The owners fallback still answers the actor question exactly as before.
    authority = RuntimeKnowledgePolicy(engine=SimpleNamespace(owners=policy.owners))
    assert authority.admin_actor() == PHONE


@pytest.mark.parametrize("entry", ["4910000000777", "whatsapp:", ":100000001"])
def test_admin_principals_rejects_unqualified_entries(entry: str) -> None:
    with pytest.raises(ValueError):
        PolicyConfig.model_validate({"admin_principals": [entry]})


def test_admin_principals_rejects_duplicates() -> None:
    with pytest.raises(ValueError):
        PolicyConfig.model_validate({"admin_principals": [PHONE, PHONE]})


# ── actor selection ──────────────────────────────────────────────────────────


def test_actor_selection_prefers_the_chat_identifier_then_the_phone_form() -> None:
    forms = {PHONE, LID}

    assert select_owner_principal(forms, LID_VALUE) == LID
    assert select_owner_principal(forms, PHONE_CHAT) == PHONE
    assert select_owner_principal(forms, GROUP_CHAT) == PHONE
    assert select_owner_principal({LID}, GROUP_CHAT) == LID
    assert select_owner_principal(set()) == ""


def test_declared_principal_is_restricted_to_its_own_channel() -> None:
    authority = RuntimeKnowledgePolicy(
        engine=None, admin_principals=frozenset({PHONE, "telegram:100000001"})
    )

    assert authority.admin_principals_for("whatsapp") == (PHONE,)
    assert authority.admin_principals_for("telegram") == ("telegram:100000001",)
    # Without a channel the historical first-declared answer is kept for old callers.
    assert authority.admin_actor() == "telegram:100000001"
    assert authority.admin_actor(channel="whatsapp") == PHONE


# ── membership: the owner widening must never reach a non-owner ──────────────


def _membership_scope(authority: RuntimeKnowledgePolicy) -> SimpleNamespace:
    return SimpleNamespace(
        identity=SimpleNamespace(_policy=authority),
        queries=SimpleNamespace(
            members=lambda **kwargs: SimpleNamespace(
                status="known", members=frozenset(), snapshot_id="snap"
            ),
            is_owner_direct_chat=lambda *args, **kwargs: True,
            resolve_identifier=lambda *args, **kwargs: "contact-1",
            snapshot=SimpleNamespace(generation="gen-1"),
        ),
    )


def _with_scope(monkeypatch, authority: RuntimeKnowledgePolicy) -> None:
    monkeypatch.setattr(
        "yeoman_gateway.knowledge._history_identity.current_history_scope",
        lambda *args, **kwargs: _membership_scope(authority),
    )


def test_owner_membership_names_every_proven_owner_form(monkeypatch) -> None:
    authority = RuntimeKnowledgePolicy(
        engine=None, admin_principals=frozenset({PHONE, LID})
    )
    _with_scope(monkeypatch, authority)
    context = TrustedReadContext(
        principal_id=LID,
        channel="whatsapp",
        chat_id=LID_VALUE,
        recipient_principals=frozenset({PHONE, LID}),
        membership_revision="owner",
        policy_revision=1,
        purpose="admin",
        now_ms=1,
        is_direct=True,
        owner=True,
    )

    membership = authority.membership(context)

    assert membership is not None
    assert membership.members == frozenset({PHONE, LID})


def test_non_owner_membership_does_not_admit_owner_forms(monkeypatch) -> None:
    authority = RuntimeKnowledgePolicy(
        engine=None, admin_principals=frozenset({PHONE, LID})
    )
    _with_scope(monkeypatch, authority)
    stranger_value = STRANGER.partition(":")[2]
    context = TrustedReadContext(
        principal_id=STRANGER,
        channel="whatsapp",
        chat_id=f"{stranger_value}@s.whatsapp.net",
        # A non-owner context that even names the owner forms does not gain them.
        recipient_principals=frozenset({PHONE, LID, STRANGER}),
        membership_revision="member",
        policy_revision=1,
        purpose="reply",
        now_ms=1,
        is_direct=True,
        owner=False,
    )

    membership = authority.membership(context)

    assert membership is not None
    assert membership.members == frozenset({STRANGER})


# ── expansion, on a store with a real identity scope ─────────────────────────


@pytest.fixture
def owner_store(tmp_path):
    """A store whose owner person is proven under a phone JID and a LID."""
    from tests.gateway.knowledge.conftest import ProjectedKnowledgeHarness

    harness = ProjectedKnowledgeHarness(tmp_path)
    try:
        # One attested contact carrying both identifiers: that attestation is the proof
        # that the phone JID and the LID name the same human.
        harness.history.add_contact(
            identifiers=["4910000000001@s.whatsapp.net", LID_VALUE],
            name="Owner",
            timestamp=harness.clock.now_ms(),
        )
        yield harness
    finally:
        harness.close()


DECLARED_OWNER = "whatsapp:4910000000001"


def test_owner_read_context_carries_every_proven_form(owner_store) -> None:
    context = owner_store.service.owner_read_context(
        channel="whatsapp", chat_id=LID_VALUE
    )

    assert context.recipient_principals == frozenset({DECLARED_OWNER, LID})
    assert context.principal_id == LID
    assert context.owner


def test_lid_chat_and_phone_chat_resolve_to_the_same_human(owner_store) -> None:
    lid_chat = owner_store.service.owner_read_context(
        channel="whatsapp", chat_id=LID_VALUE
    )
    phone_chat = owner_store.service.owner_read_context(
        channel="whatsapp", chat_id="4910000000001@s.whatsapp.net"
    )

    assert lid_chat.recipient_principals == phone_chat.recipient_principals
    # The actor follows the chat's own identifier space; the set stays identical.
    assert lid_chat.principal_id == LID
    assert phone_chat.principal_id == DECLARED_OWNER


def test_unproven_principal_does_not_widen_the_set(owner_store) -> None:
    owner_store.policy.admin_principals_for = lambda channel: (UNPROVEN,)

    context = owner_store.service.owner_read_context(
        channel="whatsapp", chat_id=LID_VALUE
    )

    # No binding proves this principal, so it stands alone.
    assert context.recipient_principals == frozenset({UNPROVEN})
    assert context.principal_id == UNPROVEN


# ── reads: an owner context reaches its own author-only rows ─────────────────


def _capture_owner_only(harness, text: str, *, author_principal: str) -> str:
    """Capture one author-only statement whose stored author is ``author_principal``."""
    harness._principals["owner-person"] = author_principal
    source = harness.source("owner-person", chat=LID_VALUE, author_only=True)
    captured = harness.capture_text(text, source)
    assert captured.ok, captured.rejected
    return captured.statement_ids[0]


def test_owner_context_reads_rows_authored_under_the_chat_identifier(owner_store) -> None:
    statement_id = _capture_owner_only(
        owner_store, "Shanghai trip is confirmed", author_principal=LID
    )
    context = owner_store.service.owner_read_context(
        channel="whatsapp", chat_id=LID_VALUE
    )

    result = owner_store.recall_text("Shanghai", context)

    assert result.statement_ids == (statement_id,)


def test_non_owner_cannot_read_another_authors_author_only_statement(owner_store) -> None:
    statement_id = _capture_owner_only(
        owner_store, "Shanghai trip is confirmed", author_principal=LID
    )
    stranger_person = owner_store.person("Stranger")
    stranger = owner_store.principal_for(stranger_person)
    stranger_context = owner_store.read_context(
        stranger_person,
        chat=LID_VALUE,
        recipients={stranger},
        is_direct=True,
        owner=False,
    )

    result = owner_store.recall_text("Shanghai", stranger_context)

    assert statement_id not in result.statement_ids
    assert not result.statement_ids
