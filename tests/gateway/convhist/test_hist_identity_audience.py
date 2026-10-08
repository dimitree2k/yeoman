import json

import pytest
from test_hist_queries import case as query_case
from test_hist_queries import contact, event, identifier, message, queries

case = query_case


def test_terminal_generated_lineage_and_curated_uuid(case):
    db, snapshot = case
    curated = "11111111-1111-4111-8111-111111111111"
    contact(db, curated)
    contact(db, "generated2", curated)
    contact(db, "generated1", "generated2")
    contact(db, "missing", "absent")
    contact(db, "cycle1", "cycle2")
    contact(db, "cycle2", "cycle1")
    q = queries(snapshot)
    assert q.terminal("generated1") == curated
    assert q.contact("generated1")["contact_id"] == curated
    assert q.terminal("missing") is None and q.contact("absent") is None
    from yeoman_gateway.history.live import HistoryPaused
    with pytest.raises(HistoryPaused, match="contact_redirect_cycle"):
        q.terminal("cycle1")
    identifier(db, "a", "10001@lid")
    identifier(db, "b", "10001@lid")
    assert q.resolve_identifier("10001@lid", at_ms=100, time_basis="native") is None


def test_identifier_half_open_windows_and_approximate_time(case):
    db, snapshot = case
    identifier(db, "a", "10001@lid", end=100)
    identifier(db, "b", "10001@lid", start=100)
    q = queries(snapshot)
    assert q.resolve_identifier("10001@lid", at_ms=99, time_basis="native") == "a"
    assert q.resolve_identifier("10001@lid", at_ms=100, time_basis="provider_timestamp") == "b"
    assert q.resolve_identifier("10001@lid", at_ms=None, time_basis="unknown") is None
    assert q.resolve_identifier("10001@lid", at_ms=101, time_basis="capture_time_approx") is None
    assert q.resolve_identifier("10001@lid", at_ms=99, time_basis="capture_time_approx") == "a"
    assert q.resolve_identifier("10001", at_ms=99, time_basis="native") is None
    identifier(db, "a", "Name", kind="push_name", strength="weak")
    assert q.resolve_identifier("Name", at_ms=99, time_basis="native") is None


def roster(db, eid="roster", ms=100, participants=None):
    event(db, eid, "member_snapshot", ms,
          {"complete": True, "participants": participants if participants is not None else
           [["10001@lid", "10001@s.whatsapp.net"], ["10002@s.whatsapp.net"]]})


def test_audience_does_not_backdate_snapshot_or_widen_on_redirect(case, tmp_path):
    db, snapshot = case
    identifier(db, "a", "10001@lid")
    identifier(db, "a", "10001@s.whatsapp.net")
    identifier(db, "b", "10002@s.whatsapp.net")
    contact(db, "c")
    identifier(db, "c", "10003@s.whatsapp.net")
    message(db, "early", ms=99)
    message(db, "known", ms=110)
    roster(db)
    event(db, "add", "member_add", 120, {"participants": [["10003@s.whatsapp.net"]]})
    event(db, "remove", "member_remove", 130, {"participants": [["10002@s.whatsapp.net"]]})
    contact(db, "d")
    identifier(db, "d", "10004@s.whatsapp.net")
    db.execute("UPDATE contacts SET merged_into='d' WHERE contact_id='a'")
    q = queries(snapshot)
    assert q.audience("early").status == "unknown"
    from tests.gateway.knowledge.conftest import KnowledgeHarness
    h = KnowledgeHarness(tmp_path / "synthetic-knowledge")
    try:
        author = h.person("Synthetic author")
        source = h.source(author)
        h.service.capture(h.candidate("synthetic secret", source), context=h.capture_context(source))
        h.authority.audiences[source.key] = q.audience("early")
        denied = h.recall_text("synthetic secret", h.read_context(author, chat="other@g.us"))
        assert "synthetic secret" not in denied.text
    finally:
        h.close()
    proof = q.audience("known")
    assert proof.status == "known"
    assert proof.members == {"whatsapp:10001", "whatsapp:10002"}
    assert q.members(chat_id="g@g.us", at_ms=140).members == {"whatsapp:10001", "whatsapp:10003"}
    assert proof.members & q.members(chat_id="g@g.us", at_ms=140).members == {"whatsapp:10001"}
    assert "whatsapp:10004" not in proof.members
    assert q.members(chat_id="g@g.us", at_ms=None).status == "unknown"
    roster(db, "conflict", participants=[["10003@s.whatsapp.net"]])
    assert q.audience("known").status == "unknown"


def test_audience_unknown_time_roster_identity_and_dm(case):
    db, snapshot = case
    identifier(db, "a", "10001@s.whatsapp.net")
    identifier(db, "b", "10002@s.whatsapp.net")
    contact(db, "owner", role="owner")
    identifier(db, "owner", "10009@s.whatsapp.net")
    message(db, "dm", chat="10002@s.whatsapp.net")
    q = queries(snapshot)
    assert q.audience("dm").members == {"whatsapp:10001", "whatsapp:10002"}
    message(db, "unknown-dm", chat="99999@lid")
    assert q.audience("unknown-dm").status == "unknown"
    message(db, "missing-time", ms=None)
    assert q.audience("missing-time").status == "unknown"
    message(db, "approx", ms=110)
    db.execute("UPDATE messages SET time_certainty='capture_time_approx' WHERE message_id='approx'")
    roster(db)
    assert q.audience("approx").status == "unknown"
    roster(db, "unresolved", ms=120, participants=[["99999@lid"]])
    assert q.members(chat_id="g@g.us", at_ms=125).members == {"whatsapp:99999@lid"}
    roster(db, "incomplete", ms=130, participants=[])
    db.execute('UPDATE message_events SET payload_json=\'{"complete":false,"participants":[]}\' WHERE event_id=\'incomplete\'')
    assert q.members(chat_id="g@g.us", at_ms=135).status == "unknown"
    roster(db, "valid", ms=140, participants=[["10001@s.whatsapp.net"]])
    assert q.members(chat_id="g@g.us", at_ms=145).members == {"whatsapp:10001"}
    event(db, "untimed", "member_remove", None, {"participants": [["10001@s.whatsapp.net"]]},
          certainty="unknown")
    assert q.members(chat_id="g@g.us", at_ms=145).status == "unknown"


def test_mentions_refuse_unknown_lid_and_multiple_phone_targets(case):
    db, snapshot = case
    identifier(db, "a", "10001@lid")
    identifier(db, "a", "10001@s.whatsapp.net")
    identifier(db, "b", "10002@s.whatsapp.net")
    roster(db)
    q = queries(snapshot)
    assert q.mention("10001@lid", chat_id="g@g.us", at_ms=110) == "10001@s.whatsapp.net"
    assert q.mention("99999@lid", chat_id="g@g.us", at_ms=110) is None
    assert q.mention("a", chat_id="g@g.us", at_ms=110) is None
    assert q.mention("10001", chat_id="g@g.us", at_ms=110) is None
    assert q.mention("10001@lid", chat_id="other@g.us", at_ms=110) is None
    identifier(db, "a", "10003@s.whatsapp.net", start=120)
    assert q.mention("10001@lid", chat_id="g@g.us", at_ms=125) is None
    db.execute("UPDATE identifier_history SET valid_until_ms=115 WHERE value='10001@lid'")
    assert q.mention("10001@lid", chat_id="g@g.us", at_ms=116) is None
    db.execute("UPDATE identifier_history SET valid_until_ms=NULL WHERE value='10001@lid'")
    event(db, "remove", "member_remove", 130, {"participants": [["10001@lid", "10001@s.whatsapp.net"]]})
    contact(db, "redirect", "a")
    identifier(db, "redirect", "10004@lid")
    assert q.mention("10004@lid", chat_id="g@g.us", at_ms=140) is None
    identifier(db, "b", "10001@lid")
    assert q.mention("10001@lid", chat_id="g@g.us", at_ms=110) is None


def test_remove_lid_retracts_paired_phone_principal(case):
    db, snapshot = case
    identifier(db, "a", "10001@lid")
    identifier(db, "a", "10001@s.whatsapp.net")
    roster(db, participants=[["10001@lid", "10001@s.whatsapp.net"]])
    event(db, "remove-lid", "member_remove", 120, {"participants": [["10001@lid"]]})
    q = queries(snapshot)
    assert q.members(chat_id="g@g.us", at_ms=110).members == {"whatsapp:10001"}
    assert q.members(chat_id="g@g.us", at_ms=125).members == frozenset()
    assert q.mention("10001@lid", chat_id="g@g.us", at_ms=125) is None


def test_mention_invalid_phone_and_expired_phone_are_withheld(case):
    db, snapshot = case
    identifier(db, "a", "10001@lid")
    identifier(db, "a", "invalid@s.whatsapp.net")
    roster(db, participants=[["10001@lid"]])
    q = queries(snapshot)
    assert q.mention("10001@lid", chat_id="g@g.us", at_ms=110) is None
    db.execute("DELETE FROM identifier_history WHERE kind='pn_jid'")
    identifier(db, "a", "10001@s.whatsapp.net", end=105)
    assert q.mention("10001@lid", chat_id="g@g.us", at_ms=110) is None


def test_approx_capture_snapshot_valid_forward_only(case):
    db, snapshot = case
    roster(db)
    db.execute("UPDATE message_events SET time_certainty='capture_time_approx'")
    message(db, "before-capture", ms=99)
    message(db, "at-capture", ms=100)
    message(db, "after-capture", ms=101)
    q = queries(snapshot)
    assert q.audience("before-capture").status == "unknown"
    for mid in ("at-capture", "after-capture"):
        proof = q.audience(mid)
        assert proof.status == "known"
        assert proof.members == {"whatsapp:10001", "whatsapp:10002"}
    db.execute("UPDATE message_events SET time_certainty='unknown'")
    assert q.audience("after-capture").status == "unknown"


def test_approx_removal_narrows_from_preceding_boundary(case):
    db, snapshot = case
    identifier(db, "a", "10001@lid")
    identifier(db, "a", "10001@s.whatsapp.net")
    identifier(db, "b", "10002@s.whatsapp.net")
    roster(db)
    message(db, "between", ms=120)
    event(db, "removed", "member_remove", 150, {"participants": [["10001@lid"]]},
          certainty="capture_time_approx")
    q = queries(snapshot)
    assert q.audience("between").members == {"whatsapp:10002"}
    assert q.members(chat_id="g@g.us", at_ms=100).members == {"whatsapp:10002"}
    assert q.members(chat_id="g@g.us", at_ms=99).status == "unknown"
    db.execute("UPDATE message_events SET time_certainty='provider_timestamp' WHERE event_id='removed'")
    assert q.audience("between").members == {"whatsapp:10001", "whatsapp:10002"}
    assert q.members(chat_id="g@g.us", at_ms=149).members == {"whatsapp:10001", "whatsapp:10002"}
    assert q.members(chat_id="g@g.us", at_ms=150).members == {"whatsapp:10002"}
    db.execute("UPDATE message_events SET time_certainty='capture_time_approx' WHERE event_id='removed'")
    roster(db, "new-boundary", ms=130)
    assert q.audience("between").members == {"whatsapp:10001", "whatsapp:10002"}
    assert q.members(chat_id="g@g.us", at_ms=130).members == {"whatsapp:10002"}


def test_approx_add_never_backdates(case):
    db, snapshot = case
    roster(db, participants=[["10001@s.whatsapp.net"]])
    event(db, "added", "member_add", 150, {"participants": [["10002@s.whatsapp.net"]]},
          certainty="capture_time_approx")
    q = queries(snapshot)
    assert q.members(chat_id="g@g.us", at_ms=149).members == {"whatsapp:10001"}
    assert q.members(chat_id="g@g.us", at_ms=150).members == {"whatsapp:10001", "whatsapp:10002"}
    assert q.members(chat_id="g@g.us", at_ms=151).members == {"whatsapp:10001", "whatsapp:10002"}


def test_unresolved_participant_keeps_typed_principal_and_known_audience(case):
    db, snapshot = case
    roster(db, participants=[["10001@s.whatsapp.net"], ["99999@lid"],
                             ["99998@lid", "99998@s.whatsapp.net"]])
    q = queries(snapshot)
    proof = q.members(chat_id="g@g.us", at_ms=110)
    assert proof.status == "known"
    assert proof.members == {"whatsapp:10001", "whatsapp:99999@lid", "whatsapp:99998"}
    event(db, "unresolved-remove", "member_remove", 120, {"participants": [["99998@lid"]]})
    assert q.members(chat_id="g@g.us", at_ms=125).members == {"whatsapp:10001", "whatsapp:99999@lid"}
    for invalid in ([["10001@s.whatsapp.net", "10002@s.whatsapp.net"]],
                    [["invalid@s.whatsapp.net"]], [["Name"]], [["10001"]], [["99999@lid", 3]]):
        db.execute("UPDATE message_events SET payload_json=? WHERE event_id='roster'",
                   (json.dumps({"complete": True, "participants": invalid}),))
        assert q.members(chat_id="g@g.us", at_ms=110).status == "unknown"


def test_members_query_count_is_bounded(case, monkeypatch):
    from yeoman_gateway.policy.identity import canonical_user_id

    db, snapshot = case
    participants = []
    for number in range(20000, 21510):
        cid = f"member-{number}"
        contact(db, cid)
        identifier(db, cid, f"{number}@lid")
        identifier(db, cid, f"{number}@s.whatsapp.net")
        if number < 21500:
            participants.append([f"{number}@lid", f"{number}@s.whatsapp.net"])
    roster(db, participants=participants)
    changes = []
    for offset in range(10):
        removed = [f"{20000 + offset}@lid"]
        added = [f"{21500 + offset}@lid", f"{21500 + offset}@s.whatsapp.net"]
        event(db, f"remove-{offset}", "member_remove", 110 + offset, {"participants": [removed]})
        event(db, f"add-{offset}", "member_add", 120 + offset, {"participants": [added]})
        changes.extend([("member_remove", removed), ("member_add", added)])
    q = queries(snapshot)
    # Deliberately slow per-row reference outside the measured trace window.
    reference = {}
    for values in participants:
        owner = q.resolve_identifier(values[0], at_ms=100, time_basis="native")
        reference[owner] = canonical_user_id("whatsapp", metadata={"sender_phone_jid": values[-1]})
    for kind, values in changes:
        owner = q.resolve_identifier(values[0], at_ms=140, time_basis="native")
        if kind == "member_remove":
            reference.pop(owner)
        else:
            reference[owner] = canonical_user_id("whatsapp", metadata={"sender_phone_jid": values[-1]})
    from unittest.mock import Mock

    import yeoman_gateway.history.queries as query_module

    distinct_values = {v for values in participants for v in values}
    distinct_values.update(v for _, values in changes for v in values)
    classify_spy = Mock(wraps=query_module.classify)
    canonical_spy = Mock(wraps=query_module.canonical_user_id)
    statements = []
    db.set_trace_callback(statements.append)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(query_module, "classify", classify_spy)
            patch.setattr(query_module, "canonical_user_id", canonical_spy)
            proof = q.members(chat_id="g@g.us", at_ms=140)
    finally:
        db.set_trace_callback(None)
    assert proof.status == "known"
    assert proof.members == frozenset(reference.values())
    assert len(proof.members) == 1500
    assert len(statements) <= 12, len(statements)
    assert classify_spy.call_count <= len(distinct_values)
    assert len({call.args[0] for call in classify_spy.call_args_list}) == classify_spy.call_count
    phone_values = {v for v in distinct_values if v.endswith("@s.whatsapp.net")}
    assert canonical_spy.call_count <= len(phone_values)
    statements.clear()
    db.set_trace_callback(statements.append)
    try:
        assert q.mention("20020@lid", chat_id="g@g.us", at_ms=140) == "20020@s.whatsapp.net"
    finally:
        db.set_trace_callback(None)
    assert len(statements) <= 20, len(statements)
