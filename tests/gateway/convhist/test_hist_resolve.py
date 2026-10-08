import uuid

import pytest
from yeoman_gateway.history.attestations import make, parse, seed_records
from yeoman_gateway.history.ids import Ident, classify
from yeoman_gateway.history.layer1 import Layer1Line
from yeoman_gateway.history.resolve import NAMESPACE, ContactRecord, IdentityInput, resolve

FRANK_LID, FRANK_PN = "46918273106072@lid", "4917632625469@s.whatsapp.net"


def _att(record, n=1):
    return parse(Layer1Line(f"owner/attestations.jsonl#{n}", record))


def test_frank_one_contact_from_three_spellings():
    inp = IdentityInput()
    raw = "whatsapp/2026-10.jsonl#1"
    inp.see(classify(FRANK_LID), 2000, raw)
    inp.see(classify(FRANK_PN), 2000, raw)
    inp.link(classify(FRANK_LID), classify(FRANK_PN), "native_pair", raw)
    inp.see(classify("4917632625469"), 1000, "backfill/memory.jsonl#5")
    inp.name(FRANK_PN, "Frank Taeger", 2000, raw)
    inp.contact_record(ContactRecord("945ae43e", 100, "Frank Taeger", None, "backfill/knowledge.jsonl#1"))
    inp.contact_record(ContactRecord("31025a3e", 200, None, None, "backfill/knowledge.jsonl#2"))
    inp.bind("945ae43e", classify(FRANK_PN), "backfill/knowledge.jsonl#3")
    inp.bind("31025a3e", classify(FRANK_LID), "backfill/knowledge.jsonl#4")
    res = resolve(inp)
    assert res.resolve(classify(FRANK_LID)) == ("945ae43e", "exact")
    assert res.resolve(classify("4917632625469")) == ("945ae43e", "numeric_match")
    rows = {c.contact_id: c for c in res.contacts}
    assert rows["31025a3e"].merged_into == "945ae43e"
    assert rows["945ae43e"].display_name == "Frank Taeger" and rows["945ae43e"].status == "confirmed"
    idents = {(r.kind, r.value): r for r in res.identifiers if r.contact_id == "945ae43e"}
    assert idents[("lid", FRANK_LID)].evidence == "native_pair"
    assert idents[("pn_jid", FRANK_PN)].first_seen_ms == 1000
    assert idents[("push_name", "Frank Taeger")].strength == "weak"
    assert res.review["merged_knowledge_contacts"][0]["merged"] == ["31025a3e"]


def test_ambiguous_number_stays_numeric_and_is_reviewed():
    inp = IdentityInput()
    for value in ("123456789@lid", "123456789@s.whatsapp.net", "123456789"):
        inp.see(classify(value), 1, "whatsapp/2026-10.jsonl#1")
    res = resolve(inp)
    contact, match = res.resolve(classify("123456789"))
    assert match == "exact" and contact not in (res.node_contact["123456789@lid"],
                                                res.node_contact["123456789@s.whatsapp.net"])
    assert res.review["numeric_ambiguous"] == [
        {"value": "123456789", "candidates": ["123456789@lid", "123456789@s.whatsapp.net"]}]
    assert {c.contact_id: c.status for c in res.contacts}[contact] == "provisional"


def test_wrong_binding_merges_until_owner_unmerges():
    owner_pn, test_pn, test_lid = "491757070305@s.whatsapp.net", "4915550000000@s.whatsapp.net", "99999999999999@lid"
    inp = IdentityInput()
    inp.contact_record(ContactRecord("O", 1, "Dimi", None, "backfill/knowledge.jsonl#1"))
    inp.contact_record(ContactRecord("T", 2, "Test", None, "backfill/knowledge.jsonl#2"))
    inp.bind("O", classify(owner_pn), "backfill/knowledge.jsonl#3")
    inp.bind("T", classify(test_pn), "backfill/knowledge.jsonl#4")
    inp.bind("O", classify(test_lid), "backfill/knowledge.jsonl#5")
    inp.link(classify(test_lid), classify(test_pn), "native_pair", "whatsapp/2026-10.jsonl#9")
    merged = resolve(inp)
    assert merged.resolve(classify(owner_pn))[0] == merged.resolve(classify(test_pn))[0] == "O"
    assert merged.review["merged_knowledge_contacts"]
    inp.attestations.append(_att(make("unmerge", 5, "test contact is not me", a=owner_pn, b=test_pn)))
    split = resolve(inp)
    assert split.resolve(classify(owner_pn))[0] == "O"
    assert split.resolve(classify(test_pn))[0] == "T"
    assert split.review["blocked_by_unmerge"]


def test_later_merge_or_unmerge_wins_for_the_same_pair():
    a, b = FRANK_PN, "4915550000000@s.whatsapp.net"
    for earlier, latest, should_merge in (("merge", "unmerge", False), ("unmerge", "merge", True)):
        inp = IdentityInput()
        inp.see(classify(a), 1, "raw#1")
        inp.see(classify(b), 2, "raw#2")
        inp.attestations.extend([
            _att(make(earlier, 10, "earlier intent", a=a, b=b), 10),
            _att(make(latest, 20, "later intent", a=a, b=b), 20),
        ])
        resolved = resolve(inp)
        assert (resolved.resolve(classify(a))[0] == resolved.resolve(classify(b))[0]) is should_merge


def test_effective_merge_edges_keep_effective_chronology_and_all_evidence():
    a, b, c = FRANK_PN, "4915550000000@s.whatsapp.net", "4915550000001@s.whatsapp.net"
    inp = IdentityInput()
    for value in (a, b, c):
        inp.see(classify(value), 1, "raw#1")
    records = [
        make("merge", 10, "merge AB", a=a, b=b),
        make("unmerge", 20, "unmerge AB", a=a, b=b),
        make("merge", 30, "merge AC", a=a, b=c),
        make("merge", 40, "merge AB again", a=a, b=b),
        make("unmerge", 50, "unmerge BC", a=b, b=c),
    ]
    inp.attestations.extend(_att(record, n) for n, record in enumerate(records, 1))

    resolved = resolve(inp)
    assert resolved.resolve(classify(a))[0] == resolved.resolve(classify(c))[0]
    assert resolved.resolve(classify(a))[0] != resolved.resolve(classify(b))[0]
    assert resolved.review["blocked_by_unmerge"] == [{
        "a": a, "b": b, "evidence": "owner_attested", "ref": "owner/attestations.jsonl#4",
    }] or resolved.review["blocked_by_unmerge"] == [{
        "a": b, "b": a, "evidence": "owner_attested", "ref": "owner/attestations.jsonl#4",
    }]
    refs = {ref for identifier in resolved.identifiers for ref in identifier.source_refs}
    assert {att.ref for att in inp.attestations} <= refs


def test_identifier_ended_without_ownership_is_reported_not_applied():
    inp = IdentityInput()
    inp.see(classify(FRANK_PN), 100, "raw#1")
    att = _att(make("identifier_ended", 200, "recorded end", identifier=FRANK_PN, ended_ms=150), 1)
    inp.attestations.append(att)
    resolved = resolve(inp)
    assert resolved.identifiers[0].ended_ms is None
    assert resolved.review["identifier_ended_not_applied"] == [{
        "ref": att.ref, "identifier": FRANK_PN, "ended_ms": 150,
        "resolution": "absent",
    }]


def test_seed_attestations_create_arvid_owner_matthias():
    inp = IdentityInput()
    inp.attestations.extend(_att(r, n) for n, r in enumerate(seed_records(), start=1))
    res = resolve(inp)
    arvid = str(uuid.uuid5(NAMESPACE, "4915202777685@s.whatsapp.net"))
    assert res.role_contact["assistant"] == arvid
    assert res.resolve(Ident("assistant", "service:speakup")) == (arvid, "exact")
    names = {c.display_name: c for c in res.contacts}
    assert names["Matthias Hoffmann"].status == "confirmed" and names["Dimi"].role == "owner"
    assert {r.evidence for r in res.identifiers} == {"owner_attested"}
    assert res.contact_for_anchor("4915140189391@s.whatsapp.net") == names["Matthias Hoffmann"].contact_id


def test_channel_group_provisional_and_determinism():
    inp = IdentityInput()
    inp.see(classify("120363398765432101@newsletter"), 5, "backfill/session_jsonl.jsonl#1")
    inp.see(classify("120363407395534152@g.us"), None, "whatsapp/2026-10.jsonl#2")
    inp.see(classify("120363407395534152"), 6, "backfill/journal.jsonl#3")
    inp.see(classify("4915111111111"), 7, "backfill/memory.jsonl#4")
    inp.name("4915111111111", "Tom", 7, "backfill/memory.jsonl#4")
    res = resolve(inp)
    kinds = {c.contact_id: c for c in res.contacts}
    channel = res.node_contact["120363398765432101@newsletter"]
    assert kinds[channel].kind == "channel"
    assert res.resolve(classify("120363407395534152")) == (None, "group")
    tom = res.resolve(classify("4915111111111"))[0]
    assert kinds[tom].status == "provisional" and kinds[tom].display_name == "Tom"
    assert resolve(inp) == res

A, B, TRANSFER, LID = ('491100000001@s.whatsapp.net', '491100000002@s.whatsapp.net',
                        '491100000003@s.whatsapp.net', '777000000001@lid')


def _windows(*windows):
    inp = IdentityInput()
    for n, (anchor, start, end) in enumerate(windows, 1):
        inp.attestations.append(_att(make('identifier', n, 'ownership', anchor=anchor,
            identifier=TRANSFER, valid_from_ms=start, valid_until_ms=end), n))
    return inp


def _at(res, value, ms=None, basis='provider_timestamp'):
    return res.resolve(classify(value), occurred_ms=ms, time_basis=basis)[0]


def test_identifier_windows_half_open_conflict_and_approximate_time():
    res = resolve(_windows((A, 100, 200), (B, 200, None)))
    assert _at(res, TRANSFER, 199) == _at(res, A)
    assert _at(res, TRANSFER, 200) == _at(res, B)
    assert _at(res, TRANSFER, 99) is None
    assert _at(res, TRANSFER) is None and res.contact_for_anchor(TRANSFER) is None
    assert _at(res, TRANSFER, 250, 'capture_time_approx') is None
    assert _at(res, TRANSFER, 199, 'capture_time_approx') == _at(res, A)
    assert _at(res, TRANSFER, 250, 'unknown') is None
    overlap = resolve(_windows((A, 100, 300), (B, 200, None)))
    assert _at(overlap, TRANSFER, 250) is None
    same = resolve(_windows((A, 100, 300), (A, 200, None)))
    assert _at(same, TRANSFER, 250) == _at(same, A)
    assert _at(same, TRANSFER) == _at(same, A)
    assert len([i for i in same.identifiers if i.value == TRANSFER]) == 2


def test_temporal_identifier_does_not_union_owners_through_lid():
    inp = _windows((A, 100, 200), (B, 200, None))
    inp.link(classify(LID), classify(TRANSFER), 'native_pair', 'raw#1', occurred_ms=150,
             time_basis='provider_timestamp')
    inp.link(classify(LID), classify(TRANSFER), 'native_pair', 'raw#2', occurred_ms=250,
             time_basis='provider_timestamp')
    inp.link(classify(LID), classify(TRANSFER), 'native_pair', 'raw#3')
    inp.see(classify('491100000003'), 150, 'raw#4')
    inp.name(LID, 'Shared name', 150, 'raw#1')
    inp.name(LID, 'Shared name', 250, 'raw#2')
    res = resolve(inp)
    assert _at(res, A) != _at(res, B)
    assert _at(res, LID, 150) == _at(res, A)
    assert _at(res, LID, 250) == _at(res, B)
    assert _at(res, LID) is None
    assert _at(res, '491100000003', 150) == _at(res, A)
    assert _at(res, '491100000003', 250) == _at(res, B)
    assert _at(res, '491100000003') is None
    assert any(r['ref'] == 'raw#3' for r in res.review['temporal_links_ambiguous'])
    # An old open LID assertion cannot reconnect the new phone owner either.
    inp.attestations.append(_att(make('contact', 1, 'old LID owner', identifiers=[A, LID]), 8))
    guarded = resolve(inp)
    assert _at(guarded, A) != _at(guarded, B)
    assert _at(guarded, TRANSFER, 250) == _at(guarded, B)


def test_parallel_open_identifiers_and_identifier_ended():
    inp = _windows((A, None, None))
    inp.attestations.append(_att(make('identifier_ended', 20, 'end', identifier=TRANSFER,
                                     ended_ms=200), 2))
    res = resolve(inp)
    assert _at(res, TRANSFER, 199) == _at(res, A)
    assert _at(res, TRANSFER, 200) is None
    row = next(i for i in res.identifiers if i.value == TRANSFER)
    assert row.valid_until_ms == row.ended_ms == 200
    assert set(row.source_refs) >= {a.ref for a in inp.attestations}
    assert not res.review['identifier_ended_not_applied']
    parallel = resolve(_windows((A, None, None)))
    assert _at(parallel, TRANSFER) == _at(parallel, A)
    for windows, reason in (([], 'absent'), ([(A, 100, 300), (B, 150, None)], 'multiple')):
        bad = _windows(*windows)
        bad.attestations.append(_att(make('identifier_ended', 50, 'ambiguous end',
                                          identifier=TRANSFER, ended_ms=200), 9))
        result = resolve(bad)
        assert result.review['identifier_ended_not_applied'][0]['resolution'] == reason
        assert all(i.ended_ms is None for i in result.identifiers)


def test_temporal_rebuild_preserves_knowledge_uuids_and_redirects():
    from dataclasses import replace

    import pytest
    inp = _windows((A, 100, 200), (B, 200, None))
    for n, anchor in enumerate((A, B)):
        inp.contact_record(ContactRecord(f'knowledge-{n}', n, 'original', None, f'bf#{n}'))
        inp.bind(f'knowledge-{n}', classify(anchor), f'bind#{n}')
    # Ten independent accepted merges, all UUIDs survive as live rows or redirects.
    for n in range(10):
        x, y = f'49120000{n:04}@s.whatsapp.net', f'49130000{n:04}@s.whatsapp.net'
        for ref, value, age in ((f'old-{n}', x, 1), (f'new-{n}', y, 2)):
            inp.contact_record(ContactRecord(ref, age, ref, None, f'contact#{n}'))
            inp.bind(ref, classify(value), f'bind#{n}')
        inp.attestations.append(_att(make('merge', 100+n, 'accepted merge', a=x, b=y), 10+n))
    inp.see(classify(A), 999, 'late#1')
    inp.see(classify(TRANSFER), 1, 'early#1')
    res = resolve(inp)
    assert _at(res, TRANSFER, 199) == 'knowledge-0'
    assert _at(res, TRANSFER, 200) == 'knowledge-1'
    rows = {c.contact_id: c for c in res.contacts}
    for n in range(10):
        assert rows[f'new-{n}'].merged_into == f'old-{n}'
    generated = str(uuid.uuid5(NAMESPACE, A))
    assert rows[generated].merged_into == 'knowledge-0'
    inp.attestations.append(_att(make('name', 500, 'rename', anchor=A, name='renamed'), 50))
    renamed = resolve(inp)
    assert _at(renamed, A) == 'knowledge-0' and resolve(inp) == renamed
    renamed.contacts = [replace(c, merged_into=generated) if c.contact_id == 'knowledge-0'
                        else c for c in renamed.contacts]
    with pytest.raises(ValueError, match='cycle'):
        renamed.contact_for_anchor(A)


def test_identifier_ended_accepts_open_knowledge_and_contact_bindings():
    for source in ('knowledge', 'contact'):
        inp = IdentityInput()
        if source == 'knowledge':
            inp.contact_record(ContactRecord('K', 1, 'known', None, 'contact#1'))
            inp.bind('K', classify(TRANSFER), 'binding#1')
        else:
            inp.attestations.append(_att(make('contact', 1, 'open identifiers',
                                              identifiers=[A, TRANSFER]), 1))
        inp.attestations.append(_att(make('identifier_ended', 2, 'end', identifier=TRANSFER,
                                          ended_ms=200), 2))
        res = resolve(inp)
        assert _at(res, TRANSFER, 199) is not None
        assert _at(res, TRANSFER, 200) is None
        row = next(i for i in res.identifiers if i.value == TRANSFER)
        assert row.valid_until_ms == row.ended_ms == 200
        assert not res.review['identifier_ended_not_applied']
        assert ('binding#1' if source == 'knowledge' else 'owner/attestations.jsonl#1') in row.source_refs


def test_multi_window_anchor_resolves_independently_of_assertion_order():
    from itertools import permutations

    q = '491100000004@s.whatsapp.net'
    assertions = [(A, TRANSFER, 100, 300), (A, TRANSFER, 200, None), (TRANSFER, q, None, None)]
    for ordering in permutations(assertions):
        inp = IdentityInput()
        for n, (anchor, ident, start, end) in enumerate(ordering, 1):
            inp.attestations.append(_att(make('identifier', n, 'ownership', anchor=anchor,
                identifier=ident, valid_from_ms=start, valid_until_ms=end), n))
        res = resolve(inp)
        assert res.contact_for_anchor(TRANSFER) == _at(res, A)
        assert _at(res, q) == _at(res, A)
        assert not res.review['temporal_links_ambiguous']
    bad = _windows((A, 100, 300), (B, 200, None))
    bad.attestations.append(_att(make('identifier', 3, 'ambiguous anchor', anchor=TRANSFER,
                                     identifier=q), 3))
    res = resolve(bad)
    assert _at(res, A) != _at(res, B)
    assert res.contact_for_anchor(TRANSFER) is None
    assert any(r['b'] == q for r in res.review['temporal_links_ambiguous'])


@pytest.mark.parametrize('bounds', [{}, {'valid_until_ms': 200},
                                  {'valid_from_ms': 0, 'valid_until_ms': 200}])
def test_window_generated_uuid_redirects_after_knowledge_binding(bounds):
    inp = IdentityInput()
    inp.attestations = [
        _att(make('contact', 1, 'original', identifiers=[TRANSFER]), 1),
        _att(make('identifier_ended', 2, 'end', identifier=TRANSFER, ended_ms=200), 2),
    ]
    before = resolve(inp)
    exposed = _at(before, TRANSFER, 199)
    assert exposed and _at(before, TRANSFER, 200) is None
    inp.contact_record(ContactRecord('K', 1, 'known', None, 'contact#1'))
    inp.bind('K', classify(TRANSFER), 'binding#1', **bounds)
    after = resolve(inp)
    assert _at(after, TRANSFER, 199) == 'K'
    assert _at(after, TRANSFER, 200) is None
    assert {c.contact_id: c.merged_into for c in after.contacts}[exposed] == 'K'
    assert after.terminal(exposed) == 'K'


def test_timed_links_materialize_once_and_fallback_only_when_absent(monkeypatch):
    from yeoman_gateway.history import resolve as module

    inp = IdentityInput()
    inp.link(classify(A), classify(LID), 'native_pair', 'raw#1', occurred_ms=150,
             time_basis='provider_timestamp')
    inp.links.append((A, LID, 'native_pair', 'raw#2'))
    factory = module.IdentityLink
    adapter = IdentityInput.timed_links.fget
    constructed, accessed = [], []

    def construct(*args, **kwargs):
        constructed.append(args)
        return factory(*args, **kwargs)

    def adapt(self):
        accessed.append(self)
        return adapter(self)

    monkeypatch.setattr(module, 'IdentityLink', construct)
    monkeypatch.setattr(IdentityInput, 'timed_links', property(adapt))
    res = resolve(inp)
    assert _at(res, A) == _at(res, LID)
    assert accessed == [inp]
    assert constructed == [(A, LID, 'native_pair', 'raw#2')]


@pytest.mark.parametrize('start', [None, 0])
def test_contact_window_alias_survives_explicit_identifier_assertion(start):
    inp = IdentityInput()
    inp.attestations = [
        _att(make('contact', 1, 'original', identifiers=[TRANSFER]), 1),
        _att(make('identifier_ended', 2, 'end', identifier=TRANSFER, ended_ms=200), 2),
    ]
    exposed = _at(resolve(inp), TRANSFER, 199)
    inp.contact_record(ContactRecord('K', 1, 'known', None, 'contact#1'))
    inp.bind('K', classify(A), 'binding#1')
    inp.attestations.append(_att(make('identifier', 3, 'explicit', anchor=A, identifier=TRANSFER,
                                     valid_from_ms=start, valid_until_ms=200), 3))
    after = resolve(inp)
    assert {c.contact_id: c.merged_into for c in after.contacts}[exposed] == 'K'
    assert after.terminal(exposed) == _at(after, TRANSFER, 199) == 'K'
    assert _at(after, TRANSFER, 200) is None


def test_window_aliases_survive_later_shorthand_and_merge():
    inp = _windows((A, 100, 300))
    initial = resolve(inp)
    original_node = f'window:{TRANSFER}:100:300:{A}'
    original_alias = str(uuid.uuid5(NAMESPACE, original_node))
    assert original_alias in {c.contact_id for c in initial.contacts}
    inp.attestations.append(_att(make('identifier_ended', 2, 'first end', identifier=TRANSFER,
                                     ended_ms=200), 2))
    first = resolve(inp)
    first_alias = str(uuid.uuid5(NAMESPACE, f'window:{TRANSFER}:100:200:{A}'))
    assert first_alias in {c.contact_id for c in first.contacts}
    inp.attestations.append(_att(make('identifier_ended', 3, 'earlier end', identifier=TRANSFER,
                                     ended_ms=180), 3))
    inp.contact_record(ContactRecord('K', 1, 'known', None, 'contact#1'))
    inp.bind('K', classify(B), 'binding#1')
    inp.attestations.append(_att(make('merge', 4, 'accepted', a=A, b=B), 4))
    after = resolve(inp)
    for alias in (original_alias, first_alias):
        assert alias in {c.contact_id for c in after.contacts}
        assert after.terminal(alias) == 'K'
    assert _at(after, TRANSFER, 179) == 'K'
    assert _at(after, TRANSFER, 180) is None


@pytest.mark.parametrize('second_owner', [A, B])
def test_prior_lid_slice_alias_requires_one_terminal_owner(second_owner):
    inp = _windows((A, 100, 300))
    inp.link(classify(LID), classify(TRANSFER), 'native_pair', 'raw#1', occurred_ms=150,
             time_basis='provider_timestamp')
    inp.link(classify(LID), classify(TRANSFER), 'native_pair', 'raw#2', occurred_ms=250,
             time_basis='provider_timestamp')
    before = resolve(inp)
    exposed = str(uuid.uuid5(NAMESPACE, f'window:{LID}:100:300:'))
    assert exposed in {c.contact_id for c in before.contacts}
    inp.attestations.append(_att(make('identifier', 2, 'refine', anchor=second_owner,
        identifier=TRANSFER, valid_from_ms=200, valid_until_ms=300), 2))
    after = resolve(inp)
    rows = {c.contact_id: c for c in after.contacts}
    if second_owner == A:
        assert exposed in rows and after.terminal(exposed) == _at(after, A)
    else:
        assert exposed not in rows
        assert after.contact_for_anchor(TRANSFER) is None
        assert _at(after, A) != _at(after, B)
    for row in after.contacts:
        after.terminal(row.contact_id)


def test_superseded_contact_window_has_no_alias_across_two_owners():
    inp = IdentityInput()
    inp.attestations = [
        _att(make('contact', 1, 'original', identifiers=[TRANSFER]), 1),
        _att(make('identifier_ended', 2, 'end', identifier=TRANSFER, ended_ms=200), 2),
    ]
    exposed = _at(resolve(inp), TRANSFER, 199)
    for n, (anchor, start, end) in enumerate(((A, None, 100), (B, 100, 200)), 3):
        inp.attestations.append(_att(make('identifier', n, 'owner', anchor=anchor,
            identifier=TRANSFER, valid_from_ms=start, valid_until_ms=end), n))
    after = resolve(inp)
    assert exposed not in {c.contact_id for c in after.contacts}
    assert _at(after, TRANSFER, 99) == _at(after, A)
    assert _at(after, TRANSFER, 199) == _at(after, B)
    assert after.contact_for_anchor(TRANSFER) is None
    for row in after.contacts:
        after.terminal(row.contact_id)



def test_generated_id_seeds_exclude_preserved_knowledge_uuid():
    from yeoman_gateway.history.ids import classify
    from yeoman_gateway.history.resolve import ContactRecord, IdentityInput

    inp = IdentityInput()
    value = '777000000001@lid'
    preserved = str(uuid.uuid5(NAMESPACE, 'preserved-source-contact'))
    inp.contact_record(ContactRecord(preserved, 1, None, None, 'backfill/knowledge.jsonl#1'))
    inp.bind(preserved, classify(value), 'backfill/knowledge.jsonl#2')
    res = resolve(inp)
    assert preserved not in {item.contact_id for item in res.generated_ids}
    assert {item.seed for item in res.generated_ids} == {value}
    assert all(item.contact_id == str(uuid.uuid5(NAMESPACE, item.seed)) for item in res.generated_ids)
    assert all(item.source_refs for item in res.generated_ids)
