"""P3.3: the current sender's identity is structured context, in every reply mode.

Parameterised across compact/full prompt mode, DM/group and current/reply speaker.  The
confirmed preferred name must win over a changing push name, an unknown identity must
fall back neutrally, and a group roster must never leak a person from outside the chat.
"""

from __future__ import annotations

import pytest
from yeoman_gateway.knowledge.models import Identifier

PUSH_SENTINEL = "Push-Name-Sentinel-4711"
SECRET_SENTINEL = "unrelated-secret-8822"


def _context_for(h, person: str, *, chat: str, recipients: set[str], is_direct: bool):
    return h.read_context(
        person, chat=chat, recipients=recipients, is_direct=is_direct, purpose="reply"
    )


@pytest.mark.parametrize("prompt_mode", ["compact", "full"])
@pytest.mark.parametrize("chat_kind", ["dm", "group"])
@pytest.mark.parametrize("speaker_kind", ["current", "reply"])
def test_confirmed_name_reaches_sender_context(
    knowledge_harness, prompt_mode: str, chat_kind: str, speaker_kind: str
):
    """The released name is available identically in every mode combination."""
    h = knowledge_harness
    tom = h.person("Tom")
    h.service.set_preferred_name(tom, "Confirmed-Tom", context=h.admin_context())
    # A later push name must not win.
    h.observe(
        "whatsapp",
        "phone_jid",
        f"{h.principal_for(tom).split(':')[1]}@s.whatsapp.net",
        name=PUSH_SENTINEL,
    )

    is_direct = chat_kind == "dm"
    chat = "dm-tom" if is_direct else "group-a"
    recipients = {h.principal_for(tom)}
    context = _context_for(h, tom, chat=chat, recipients=recipients, is_direct=is_direct)
    other = h.person("Alex") if speaker_kind == "reply" else None
    if other is not None:
        h.service.set_preferred_name(other, "Confirmed-Alex", context=h.admin_context())

    name = h.service.display_name(tom, context=context, for_group=not is_direct)
    assert name == "Confirmed-Tom"
    assert PUSH_SENTINEL not in (name or "")

    reply_name = (
        h.service.display_name(other, context=context, for_group=not is_direct)
        if other is not None
        else None
    )
    if other is not None:
        assert reply_name == "Confirmed-Alex"


@pytest.mark.parametrize("prompt_mode", ["compact", "full"])
def test_unknown_identity_falls_back_neutrally(knowledge_harness, prompt_mode: str):
    """An unresolvable sender yields no name and no guessed person."""
    h = knowledge_harness
    before = h.snapshot_counts()
    result = h.observe("telegram", "telegram_username", f"@unknown-{prompt_mode}")
    assert result.status == "unresolved"
    assert result.person_id is None
    assert h.snapshot_counts()["contacts"] == before["contacts"]


def test_group_roster_never_leaks_an_unrelated_contact(knowledge_harness):
    """Only proven members of *this* chat feed the roster."""
    h = knowledge_harness
    tom = h.person("Tom")
    alex = h.person("Alex")
    h.service.set_preferred_name(alex, "Confirmed-Alex", context=h.admin_context())
    audience = {h.principal_for(tom), h.principal_for(alex)}

    # A person the chat has nothing to do with, with a secret fact of their own.
    outsider = h.person("Outsider")
    h.service.set_preferred_name(outsider, SECRET_SENTINEL, context=h.admin_context())
    foreign = h.source(outsider, chat="other-group")
    h.capture_text(f"{SECRET_SENTINEL} lives here", foreign)

    chat_context = _context_for(h, tom, chat="group-a", recipients=audience, is_direct=False)
    rows = h.service.roster(context=chat_context, participant_ids=tuple(sorted(audience)))
    names = [name for name, _facts in rows]
    assert "Confirmed-Alex" in names
    assert SECRET_SENTINEL not in " ".join(names)
    assert all(SECRET_SENTINEL not in " ".join(facts) for _name, facts in rows)


def test_group_roster_excludes_names_released_only_privately(knowledge_harness):
    h = knowledge_harness
    tom = h.person("Tom")
    alex = h.person("Alex")
    h.service.set_preferred_name(
        alex, "Private-Alex", context=h.admin_context(), visibility="private"
    )
    audience = {h.principal_for(tom), h.principal_for(alex)}
    group = _context_for(h, tom, chat="group-a", recipients=audience, is_direct=False)
    direct = _context_for(h, tom, chat="dm", recipients=audience, is_direct=True)

    group_names = [name for name, _ in h.service.roster(context=group, participant_ids=tuple(audience))]
    assert "Private-Alex" not in group_names
    direct_names = [
        name for name, _ in h.service.roster(context=direct, participant_ids=tuple(audience))
    ]
    assert "Private-Alex" in direct_names


def test_structured_sender_identity_keeps_the_raw_observed_name_separate(knowledge_harness):
    """Raw push data stays available, but never replaces the released name."""
    h = knowledge_harness
    person = h.observe("whatsapp", "phone_jid", "491555000111@s.whatsapp.net", name="Raw Push")
    h.service.set_preferred_name(person.person_id, "Released", context=h.admin_context())

    aliases = h.service.alias_names(person.person_id)
    assert "Raw Push" in aliases
    assert h.service.display_name(person.person_id) == "Released"
    # The identifier is still resolvable to the person for delivery decisions.
    assert h.service.person_id_for_value("491555000111@s.whatsapp.net") == person.person_id


def test_person_resolution_never_grants_owner_or_audience(knowledge_harness):
    """Resolving a person must not change any security principal or ACL epoch."""
    h = knowledge_harness
    tom = h.person("Tom")
    alex = h.person("Alex")
    audience = {h.principal_for(tom), h.principal_for(alex)}
    source = h.source(tom, audience=audience)
    sid = h.capture_text("Alex reist.", source, subjects=(alex,)).statement_ids[0]

    before_fingerprint = h.security_fingerprint()
    epoch_before = h.acl_epoch()
    revision_before = h.identity_revision()

    context = _context_for(h, tom, chat="group-a", recipients=audience, is_direct=False)
    result = h.recall_person(alex, context)
    assert result.statement_ids == (sid,)
    assert h.security_fingerprint() == before_fingerprint
    assert h.acl_epoch() == epoch_before
    assert h.identity_revision() == revision_before


def test_role_edges_do_not_appear_in_the_roster_as_audience(knowledge_harness):
    """A participant role is not a read right: the roster shows only released facts."""
    h = knowledge_harness
    tom = h.person("Tom")
    alex = h.person("Alex")
    maria = h.person("Maria")
    wide = {h.principal_for(tom), h.principal_for(alex), h.principal_for(maria)}
    narrow = {h.principal_for(tom), h.principal_for(alex)}
    source = h.source(tom, audience=narrow)
    h.capture_text("restricted fact", source, participants=(maria,))

    # Maria is a member but not in the statement's audience: the roster must not show it.
    context = _context_for(h, tom, chat="group-a", recipients=wide, is_direct=False)
    rows = h.service.roster(context=context, participant_ids=tuple(sorted(wide)))
    assert not any("restricted fact" in " ".join(facts) for _name, facts in rows)


def test_identifier_kind_is_preserved_in_structured_context(knowledge_harness):
    """Phone and LID stay distinguishable instead of collapsing into one token."""
    h = knowledge_harness
    person = h.observe("whatsapp", "phone_jid", "491555000222@s.whatsapp.net")
    h.observe(
        "whatsapp",
        "phone_jid",
        "491555000222@s.whatsapp.net",
        mapping=True,
        extra=(Identifier("whatsapp", "lid", "99900011122233@lid"),),
    )
    identifiers = {item.kind for item in h.service.person_identifiers(person.person_id)}
    assert identifiers == {"phone_jid", "lid"}
