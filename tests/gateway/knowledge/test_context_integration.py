"""P3.3: the current sender's identity is structured context, in every reply mode.

Parameterised across compact/full prompt mode, DM/group and current/reply speaker.  The
confirmed preferred name must win over a changing push name, an unknown identity must
fall back neutrally, and a group roster must never leak a person from outside the chat.
"""

from __future__ import annotations

PUSH_SENTINEL = "Push-Name-Sentinel-4711"
SECRET_SENTINEL = "unrelated-secret-8822"


def _context_for(h, person: str, *, chat: str, recipients: set[str], is_direct: bool):
    return h.read_context(
        person, chat=chat, recipients=recipients, is_direct=is_direct, purpose="reply"
    )






def test_group_roster_never_leaks_an_unrelated_contact(knowledge_harness):
    """Only proven members of *this* chat feed the roster."""
    h = knowledge_harness
    tom = h.person("Tom")
    alex = h.person("Alex")
    audience = {h.principal_for(tom), h.principal_for(alex)}

    # A person the chat has nothing to do with, with a secret fact of their own.
    outsider = h.person(SECRET_SENTINEL)
    foreign = h.source(outsider, chat="120363000000000003@g.us")
    h.capture_text(f"{SECRET_SENTINEL} lives here", foreign)

    chat_context = _context_for(h, tom, chat="group-a", recipients=audience, is_direct=False)
    rows = h.service.roster(context=chat_context, participant_ids=tuple(sorted(audience)))
    names = [name for name, _facts in rows]
    assert "Alex" in names
    assert SECRET_SENTINEL not in " ".join(names)
    assert all(SECRET_SENTINEL not in " ".join(facts) for _name, facts in rows)






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
    from yeoman_gateway.history.queries import HistoryQueries

    phone = "491555000222@s.whatsapp.net"
    lid = "99900011122233@lid"
    h.history.add_contact(
        identifiers=[phone, lid], name="Alex", timestamp=h.clock.now_ms()
    )
    with h.history_scope() as snapshot:
        queries = HistoryQueries(snapshot)
        phone_owner = queries.resolve_identifier(
            phone, at_ms=h.clock.now_ms(), time_basis="native"
        )
        lid_owner = queries.resolve_identifier(
            lid, at_ms=h.clock.now_ms(), time_basis="native"
        )
    assert phone_owner is not None
    assert lid_owner == phone_owner
    assert {item.kind for item in h.service.person_identifiers(phone_owner)} == {"phone_jid", "lid"}
