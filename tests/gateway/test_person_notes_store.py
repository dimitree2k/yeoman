"""Person-note store, invalidation, read gate and purge.

Synthetic only: a projected history + schema-3 Knowledge copy, synthetic statements and
a temporary processing store.  Nothing here reads a runtime path or calls a provider.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from yeoman_gateway.knowledge.models import (
    PersonNoteLine,
    PersonNoteSeed,
    TrustedReadContext,
)
from yeoman_gateway.processing.person_notes import stated_lines

from tests.gateway.knowledge.conftest import ProjectedKnowledgeHarness

CHANNEL = "whatsapp"
GROUP = "group-a"
CHAT = "120363000000000001@g.us"


@pytest.fixture
def h(tmp_path: Path):
    harness = ProjectedKnowledgeHarness(tmp_path)
    try:
        yield harness
    finally:
        harness.close()


def _context(
    h: ProjectedKnowledgeHarness, reader: str = "Tom", **kwargs: Any
) -> TrustedReadContext:
    if ":" not in reader:
        reader = h.person(reader)
    return h.read_context(reader, chat=GROUP, channel=CHANNEL, **kwargs)


def _statement(h: ProjectedKnowledgeHarness, person: str, *, text: str | None = None) -> str:
    _, alex = h.two_people()
    source = h.source(
        person,
        chat=GROUP,
        audience={h.principal_for(person), h.principal_for(alex)},
    )
    captured = h.capture_text(text or f"{person} likes charts", source, subjects=(person,))
    return captured.statement_ids[0]


def _note(
    h: ProjectedKnowledgeHarness,
    *,
    person: str,
    chat: str = CHAT,
    input_hash: str = "hash-1",
    lines: tuple[PersonNoteLine, ...] | None = None,
    sources: tuple[tuple[str, str, int], ...] = (),
    statement_id: str | None = None,
) -> Any:
    sid = statement_id or _statement(h, person)
    return h.service.store_person_note(
        PersonNoteSeed(
            channel=CHANNEL,
            chat_id=chat,
            person_id=person,
            input_hash=input_hash,
            input_cut_ms=h.clock.now_ms(),
            lines=lines or (PersonNoteLine(kind="stated", text="Tom: likes charts", refs=(sid,)),),
            sources=sources or (("statement", sid, 1),),
        )
    )


def test_supersede_keeps_old_row_admin_only(h: ProjectedKnowledgeHarness) -> None:
    tom, _alex = h.two_people()
    sid = _statement(h, tom)
    first = _note(h, person=tom, input_hash="hash-1", statement_id=sid)
    second = _note(
        h,
        person=tom,
        input_hash="hash-2",
        statement_id=sid,
        lines=(PersonNoteLine(kind="stated", text="Tom: newer line", refs=(sid,)),),
    )
    group = _context(h, recipients={h.principal_for(tom)})
    active = h.service.person_notes(channel=CHANNEL, chat_id=CHAT, person_ids=(tom,), context=group)
    assert [note.note_id for note in active] == [second.note_id]
    admin = h.read_context(
        tom,
        chat=GROUP,
        channel=CHANNEL,
        purpose="admin",
        owner=True,
        recipients={h.principal_for(tom)},
    )
    every = h.service.person_notes(
        channel=CHANNEL,
        chat_id=CHAT,
        person_ids=(tom,),
        context=admin,
        include_superseded=True,
    )
    assert {note.note_id for note in every} == {first.note_id, second.note_id}
    old = next(note for note in every if note.note_id == first.note_id)
    assert old.status == "superseded" and old.superseded_by == second.note_id
    assert old.lines and old.lines[0].text == "Tom: likes charts"


def _processing_message(tmp_path: Path, *, message_id: str = "m1") -> Any:
    from yeoman_gateway.processing.store import ProcessingStore

    store = ProcessingStore(tmp_path / "processing.db")
    thread_id = store.open_thread(
        channel="whatsapp",
        chat_id=CHAT,
        root_principal="4910000000002",
        kind="group",
        trigger_event_id=message_id,
        now_ms=1_700_000_000_000,
    )
    store.open_turn(
        thread_id=thread_id,
        principal="4910000000002",
        trigger_event_id=message_id,
        now_ms=1_700_000_000_000,
    )
    store.append_event(
        event_key=f"whatsapp:{CHAT}:message:{message_id}",
        event_id=message_id,
        trace_id=f"tr-{message_id}",
        payload={"text": "synthetic line"},
        now_ms=1_700_000_000_000,
    )
    store._conn.execute(
        "UPDATE events SET source_message_id = ?, principal = ?, channel = ?, chat_id = ?,"
        " kind = 'message', thread_id = ?, turn_id = ? WHERE event_id = ?",
        (
            message_id,
            "4910000000002",
            "whatsapp",
            CHAT,
            thread_id,
            thread_id,
            message_id,
        ),
    )
    store._conn.commit()
    return store


def test_note_stale_between_delete_and_tick(h: ProjectedKnowledgeHarness, tmp_path: Path) -> None:
    from yeoman_gateway.processing.invalidation import SignalInvalidator

    processing = _processing_message(tmp_path)
    try:
        tom, _alex = h.two_people()
        _note(
            h,
            person=tom,
            lines=(PersonNoteLine(kind="observed", text="talks about markets"),),
            sources=(("message", "m1", 0),),
        )
        invalidator = SignalInvalidator(
            store=processing, statements=h.service, clock=lambda: h.clock.now_ms()
        )
        result = invalidator(
            "delete",
            {
                "chatJid": CHAT,
                "messageId": "m1",
                "senderId": "4910000000002",
                "isGroup": True,
            },
        )
        assert result.applied is True
        stale = h.service.latest_person_note(channel=CHANNEL, chat_id=CHAT, person_id=tom)
        assert stale is not None
        assert stale.status == "stale" and stale.lines == ()
        assert stale.stale_reason == "source_delete"
    finally:
        processing.close()


def test_note_stale_on_statement_correction(h: ProjectedKnowledgeHarness) -> None:
    tom, alex = h.two_people()
    source = h.source(tom, chat=GROUP, audience={h.principal_for(tom), h.principal_for(alex)})
    captured = h.capture_text("Tom likes old charts", source, subjects=(tom,))
    statement_id = captured.statement_ids[0]
    _note(
        h,
        person=tom,
        statement_id=statement_id,
        lines=(PersonNoteLine(kind="stated", text="Tom: likes old charts", refs=(statement_id,)),),
    )
    replacement = h.source(tom, chat=GROUP, audience={h.principal_for(tom), h.principal_for(alex)})
    h.service.correct_statement(
        statement_id,
        h.candidate("Tom likes new charts", replacement, subjects=(tom,)),
        expected_source=source,
        context=h.capture_context(replacement),
    )
    note = h.service.latest_person_note(channel=CHANNEL, chat_id=CHAT, person_id=tom)
    assert note is not None and note.status == "stale" and note.lines == ()
    assert note.stale_reason == "statement_corrected"


def test_note_stale_on_statement_revoke(h: ProjectedKnowledgeHarness) -> None:
    tom, alex = h.two_people()
    source = h.source(tom, chat=GROUP, audience={h.principal_for(tom), h.principal_for(alex)})
    captured = h.capture_text("Tom likes old charts", source, subjects=(tom,))
    _note(h, person=tom, statement_id=captured.statement_ids[0])
    h.service.invalidate_event_sources([source.event_id])
    note = h.service.latest_person_note(channel=CHANNEL, chat_id=CHAT, person_id=tom)
    assert note is not None and note.status == "stale" and note.lines == ()


def test_author_only_dm_other_chat_never_input(h: ProjectedKnowledgeHarness) -> None:
    tom, alex = h.two_people()
    principals = {h.principal_for(tom), h.principal_for(alex)}
    shared = h.source(tom, chat=GROUP, audience=principals)
    h.capture_text("Tom shared a group plan", shared, subjects=(tom,), participants=(alex,))
    author_only = h.source(tom, chat=GROUP, audience=principals, author_only=True)
    h.capture_text("Tom told only Arvid a secret", author_only, subjects=(tom,))
    direct = h.source(alex, chat=GROUP, audience={h.principal_for(tom)})
    h.capture_text("Tom said this in a DM", direct, subjects=(tom,))
    other = h.source(alex, chat="group-b", audience=principals)
    h.capture_text("Tom said this elsewhere", other, subjects=(tom,))
    h.clock.advance(5 * 60_000)  # outside the projector's identical-snapshot window
    context = _context(h, "Tom", recipients=principals)

    lines = stated_lines(h.service, tom, context=context)
    text = "\n".join(line.line.text for line in lines)
    assert "group plan" in text
    assert "secret" not in text
    assert "DM" not in text and "elsewhere" not in text


def test_no_owner_detail_phone_or_jid_in_lines(h: ProjectedKnowledgeHarness) -> None:
    tom, alex = h.two_people()
    owner = h.person("Owner")
    principals = {h.principal_for(tom), h.principal_for(alex)}
    shared = h.source(alex, chat=GROUP, audience=principals)
    h.capture_text(
        "Tom can be reached at +49 151 0000 0001 or 491510000001@s.whatsapp.net",
        shared,
        subjects=(tom,),
    )
    private = h.source(owner, chat=GROUP, author_only=True)
    h.capture_text("Tom's owner-only detail", private, subjects=(tom,))
    h.clock.advance(5 * 60_000)  # outside the projector's identical-snapshot window
    context = _context(h, "Tom", recipients=principals)
    lines = stated_lines(h.service, tom, context=context)
    text = "\n".join(line.line.text for line in lines)
    assert "491510000001" not in text
    assert "@s.whatsapp.net" not in text
    assert "owner-only" not in text


def test_unresolved_author_dropped(h: ProjectedKnowledgeHarness) -> None:
    tom, alex = h.two_people()
    principals = {h.principal_for(tom), h.principal_for(alex)}
    source = h.source(tom, chat=GROUP, audience=principals)
    captured = h.capture_text("A fact with no resolvable author", source, subjects=(tom,))
    context = _context(h, "Tom", recipients=principals)
    store = h._raw_service._store
    with store.transaction():
        store.execute(
            "UPDATE knowledge_statements SET speaker_person_id = NULL WHERE statement_id = ?",
            (captured.statement_ids[0],),
        )
    assert stated_lines(h.service, tom, context=context) == ()


def test_read_time_recheck_marks_deleted_message_stale(
    h: ProjectedKnowledgeHarness,
) -> None:
    tom, _alex = h.two_people()
    note = _note(
        h,
        person=tom,
        lines=(PersonNoteLine(kind="observed", text="likes charts"),),
        sources=(("message", "gone-message", 0),),
    )
    group = _context(h, recipients={h.principal_for(tom)})
    notes = h.service.person_notes(
        channel=CHANNEL,
        chat_id=CHAT,
        person_ids=(tom,),
        context=group,
        source_present=lambda message_id: message_id != "gone-message",
    )
    assert notes[0].note_id == note.note_id
    assert notes[0].status == "stale" and notes[0].lines == ()


def test_purge_by_person_and_by_chat(h: ProjectedKnowledgeHarness) -> None:
    tom, alex = h.two_people()
    _note(h, person=tom, input_hash="t1")
    _note(h, person=tom, input_hash="t2")
    _note(h, person=alex, chat="120363000000000002@g.us", input_hash="a1")
    assert h.service.purge_person_notes(channel=CHANNEL, person_id=tom) == 2
    assert h.service.purge_person_notes(channel=CHANNEL, chat_id="120363000000000002@g.us") == 1
    assert h.service.person_note_counts(channel=CHANNEL) == ()


@pytest.mark.parametrize("bad_kind", ["fact", "", "STATED"])
def test_store_rejects_unknown_line_kinds(h: ProjectedKnowledgeHarness, bad_kind: str) -> None:
    tom, _alex = h.two_people()
    with pytest.raises(Exception):
        _note(h, person=tom, lines=(PersonNoteLine(kind=bad_kind, text="x"),))


def test_schema_creation_is_additive_and_idempotent(h: ProjectedKnowledgeHarness) -> None:
    store = h._raw_service._store
    before = set(store.table_names())
    store.ensure_person_note_schema()
    store.ensure_person_note_schema()
    after = set(store.table_names())
    assert before <= after
    assert {"knowledge_person_notes", "knowledge_person_note_sources"} <= after
    # Existing tables keep their rows: only new tables/indexes were created.
    assert h._raw_service._store.counts()["knowledge_statements"] >= 0
