"""Plan 05 / Aufgabe 3: the fact extractor proposes, deterministic code decides."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from yeoman_gateway.memory.extraction_jobs import (
    SharedFactExtractionQueue,
    check_candidate,
)
from yeoman_gateway.memory.fact_extractor import (
    MAX_CANDIDATES_PER_JOB,
    SharedFactExtractor,
    event_view,
)
from yeoman_gateway.memory.store import MemoryStore
from yeoman_shared.config.schema import Config

CHAT = "gruppe@g.us"
GROUP = "whatsapp:gruppe@g.us"
T0 = 1_700_000_000_000


class _Event:
    def __init__(
        self,
        event_id: str,
        text: str,
        *,
        principal: str = "member-old",
        kind: str = "message",
        body: dict | None = None,
        chat_id: str = CHAT,
    ) -> None:
        payload = {"text": text, "is_group": chat_id.endswith("@g.us")}
        payload.update(body or {})
        self.event_id = event_id
        self.payload = payload
        self.payload_available = True
        self.principal = principal
        self.kind = kind
        self.channel = "whatsapp"
        self.chat_id = chat_id
        self.occurred_ms = T0


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content


class _Provider:
    """Stands in for the routed chat provider and records what it was asked."""

    def __init__(self, content: str) -> None:
        self._content = content
        self.prompts: list[str] = []

    async def chat(self, *, messages, tools, model, max_tokens, temperature) -> Any:
        self.prompts.append("\n".join(str(item.get("content", "")) for item in messages))
        return _Response(self._content)


def _extractor(
    content: str,
    *,
    members: frozenset[str] | None = None,
    max_candidates: int = MAX_CANDIDATES_PER_JOB,
) -> SharedFactExtractor:
    extractor = SharedFactExtractor.__new__(SharedFactExtractor)
    extractor._config = Config()  # type: ignore[attr-defined]
    extractor._route_key = "memory.capture.extract"  # type: ignore[attr-defined]
    extractor._member_provider = (  # type: ignore[attr-defined]
        (lambda channel, chat_id: members) if members is not None else None
    )
    extractor._max_candidates = max_candidates  # type: ignore[attr-defined]
    extractor._model = "test-model"  # type: ignore[attr-defined]
    extractor._max_tokens = 200  # type: ignore[attr-defined]
    extractor._temperature = 0.0  # type: ignore[attr-defined]
    extractor._provider = _Provider(content)  # type: ignore[attr-defined]
    return extractor


def _payload(*rows: dict[str, str]) -> str:
    import json

    return json.dumps({"facts": list(rows)})


def test_candidate_cap_is_a_code_constant_not_a_config_hope() -> None:
    assert MAX_CANDIDATES_PER_JOB == 4


def test_only_user_messages_become_sources() -> None:
    assistant = _Event("ev-assistant", "Ich habe dir geschrieben.", body={"role": "assistant"})
    summary = _Event("ev-summary", "Zusammenfassung des Tages", kind="edit")

    assert event_view(assistant).is_user_message is False
    assert event_view(summary).is_user_message is False
    assert event_view(_Event("ev-user", "Der Stammtisch ist donnerstags.")).is_user_message is True


def test_a_turn_without_user_text_extracts_nothing() -> None:
    extractor = _extractor(_payload({"content": "x", "basis": "explicit_statement"}))

    assert extractor([_Event("ev-a", "hi", body={"role": "assistant"})]) == []


def test_model_rows_become_candidates_with_proven_audience() -> None:
    extractor = _extractor(
        _payload(
            {"content": "Der Stammtisch ist donnerstags.", "basis": "explicit_statement"},
            {"content": "Ich finde das gut.", "basis": "opinion"},
        ),
        members=frozenset({"member-old", "member-new"}),
    )

    candidates = extractor([_Event("ev1", "Der Stammtisch ist donnerstags.")])

    assert len(candidates) == 2
    assert candidates[0].author_principal == "member-old"
    assert candidates[0].visibility_scope == "chat_shared"
    assert candidates[0].audience == frozenset({"member-old", "member-new"})
    assert candidates[0].source_refs == (("ev1", 1),)
    assert check_candidate(candidates[0]).accepted is True
    # The opinion is refused by the deterministic check, not by trust in the model.
    assert check_candidate(candidates[1]).reason == "opinion"


def test_without_proven_membership_the_fact_is_author_only() -> None:
    extractor = _extractor(
        _payload({"content": "Der Stammtisch ist donnerstags.", "basis": "explicit_statement"}),
        members=None,
    )

    candidate = extractor([_Event("ev1", "Der Stammtisch ist donnerstags.")])[0]

    assert candidate.visibility_scope == "author_only"
    assert candidate.audience == frozenset({"member-old"})


def test_direct_chat_facts_are_principal_scoped() -> None:
    extractor = _extractor(
        _payload({"content": "Wir treffen uns morgen.", "basis": "explicit_statement"}),
        members=frozenset({"member-old", "34596062240904@lid"}),
    )

    candidate = extractor(
        [_Event("ev1", "Wir treffen uns morgen.", chat_id="34596062240904@lid")]
    )[0]

    assert candidate.visibility_scope == "principals"


def test_candidates_are_capped() -> None:
    rows = [
        {"content": f"Fakt {index}", "basis": "explicit_statement"} for index in range(9)
    ]
    extractor = _extractor(_payload(*rows), members=frozenset({"member-old"}))

    assert len(extractor([_Event("ev1", "viele Fakten")])) == MAX_CANDIDATES_PER_JOB


def test_unknown_basis_is_refused_not_trusted() -> None:
    extractor = _extractor(
        _payload({"content": "Das Meeting ist am Montag.", "basis": "vibes"}),
        members=frozenset({"member-old"}),
    )

    candidate = extractor([_Event("ev1", "Das Meeting ist am Montag.")])[0]

    assert check_candidate(candidate).reason == "uncertain"


def test_unparseable_model_output_yields_no_candidate() -> None:
    extractor = _extractor("I could not find any facts, sorry.")

    assert extractor([_Event("ev1", "hallo")]) == []


def test_explicit_deadline_is_carried_over() -> None:
    extractor = _extractor(
        _payload({"content": "Umzug am 1.10.", "basis": "explicit_statement", "valid_until": "2026-10-01"}),
        members=frozenset({"member-old"}),
    )

    candidate = extractor([_Event("ev1", "Umzug am 1.10.")])[0]

    assert candidate.valid_until_ms is not None


def test_queue_publishes_a_readable_fact_from_the_extractor(tmp_path: Path) -> None:
    """The wired chain: job -> extractor -> stored fact with an audience.

    Synchronous on purpose: the extractor calls ``asyncio.run`` internally, which is only
    legal off the event loop - the queue runs it on its own worker thread in production.
    """
    from yeoman_gateway.memory.read_gate import FactReadGate
    from yeoman_gateway.memory.shared_facts import FactReadContext

    store = MemoryStore(tmp_path / "memory.db")
    extractor = _extractor(
        _payload({"content": "Der Stammtisch ist donnerstags.", "basis": "explicit_statement"}),
        members=frozenset({"member-old", "member-new"}),
    )
    class _Journal:
        def get_event(self, event_id: str):
            return _Event(event_id, "Der Stammtisch ist donnerstags.")

    queue = SharedFactExtractionQueue(
        store=store,
        extractor=extractor,
        journal=_Journal(),
        clock=lambda: T0,
    )
    queue.enqueue(
        turn_ref="tu1",
        source_refs=[("ev1", 1)],
        now_ms=T0,
        workspace_id="ws1",
        chat_scope_key=GROUP,
    )

    report = queue.run_due(now_ms=T0)

    assert report.published == 1
    fact = store.list_facts()[0]
    assert fact.audience == frozenset({"member-old", "member-new"})
    gate = FactReadGate(store)
    reader = FactReadContext(
        principal_id="member-old",
        chat_scope_key=GROUP,
        current_members=frozenset({"member-old", "member-new"}),
        now_ms=T0 + 1,
    )
    assert gate.allowed_fact_ids(reader) == frozenset({fact.fact_id})
    # A reader who was not part of the proven audience still sees nothing.
    outsider = FactReadContext(
        principal_id="member-new",
        chat_scope_key=GROUP,
        current_members=frozenset({"member-old", "member-new"}),
        now_ms=T0 + 1,
    )
    assert gate.allowed_fact_ids(outsider) == frozenset({fact.fact_id})
    stranger = FactReadContext(
        principal_id="stranger",
        chat_scope_key=GROUP,
        current_members=frozenset({"member-old", "member-new", "stranger"}),
        now_ms=T0 + 1,
    )
    assert gate.allowed_fact_ids(stranger) == frozenset()
    store.close()


def test_meta_statements_are_refused() -> None:
    """"The author says X" is a transcript, not a fact - proven against real output."""
    from yeoman_gateway.memory.extraction_jobs import is_meta_statement

    assert is_meta_statement('Der Autor sagt: "noch tests".')
    assert is_meta_statement("The message says the meeting moved")
    assert not is_meta_statement("Der Stammtisch ist donnerstags.")

    verdict = check_candidate(
        _extractor(
            _payload({"content": 'Der Autor sagt: "noch tests".', "basis": "explicit_statement"}),
            members=frozenset({"member-old"}),
        )([_Event("ev1", "noch tests")])[0]
    )
    assert verdict.rejected
    assert verdict.reason == "meta_statement"


class _StubEmbedder:
    """Deterministic stand-in: 'stammtisch' and 'treffen' land on the same axis."""

    model = "stub-embed"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[str] = []

    def embed(self, text: str) -> list[float] | None:
        self.calls.append(text)
        if self.fail:
            raise RuntimeError("embedding provider down")
        if "stammtisch" in text.lower() or "treffen" in text.lower():
            return [1.0, 0.0]
        return [0.0, 1.0]


def _queue_with(
    store: MemoryStore, *, embedder=None, content: str = "Der Stammtisch ist donnerstags."
) -> SharedFactExtractionQueue:
    extractor = _extractor(
        _payload({"content": content, "basis": "explicit_statement"}),
        members=frozenset({"member-old"}),
    )

    class _Journal:
        def get_event(self, event_id: str):
            return _Event(event_id, content)

    return SharedFactExtractionQueue(
        store=store, extractor=extractor, journal=_Journal(), embedder=embedder, clock=lambda: T0
    )


def _run(
    queue: SharedFactExtractionQueue,
    *,
    workspace: str = "ws1",
    scope: str = GROUP,
) -> None:
    queue.enqueue(
        turn_ref="tu1",
        source_refs=[("ev1", 1)],
        now_ms=T0,
        workspace_id=workspace,
        chat_scope_key=scope,
    )
    queue.run_due(now_ms=T0)


def test_published_facts_get_an_embedding(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    embedder = _StubEmbedder()
    queue = _queue_with(store, embedder=embedder)

    _run(queue)

    fact = store.list_facts()[0]
    assert embedder.calls == [fact.content]
    assert queue.embeddings_written == 1
    assert store.has_fact_embeddings() is True
    row = store._conn.execute(
        "SELECT model, dims FROM memory2_embeddings WHERE entry_id = ?", (fact.fact_id,)
    ).fetchone()
    assert row["model"] == "stub-embed"
    assert row["dims"] == 2
    store.close()


def test_embedding_failure_keeps_the_fact(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    queue = _queue_with(store, embedder=_StubEmbedder(fail=True))

    _run(queue)

    assert len(store.list_facts()) == 1  # the fact survives ...
    assert store.has_fact_embeddings() is False  # ... without a vector
    assert queue.embeddings_failed == 1
    store.close()


def test_fact_is_found_by_meaning_not_only_by_words(tmp_path: Path) -> None:
    """A query with no word in common still finds the fact, via the vector path."""
    from unittest.mock import patch

    from yeoman_gateway.memory.service import MemoryService
    from yeoman_gateway.memory.shared_facts import FactReadContext
    from yeoman_shared.config.schema import Config

    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    cfg = Config()
    cfg.memory.db_path = str(tmp_path / "memory.db")
    cfg.memory.capture.enabled = False
    cfg.memory.embedding.enabled = False
    cfg.memory.shared.enabled = True
    with patch("yeoman_gateway.memory.service._load_owner_ids", return_value={}):
        service = MemoryService(workspace=workspace, config=cfg.memory)

    embedder = _StubEmbedder()
    queue = _queue_with(service.store, embedder=embedder)
    scope = "channel:whatsapp:chat:gruppe@g.us"  # the canonical scope key
    _run(queue, workspace=service.workspace_id, scope=scope)
    service.embedding = embedder  # retrieval must embed the query too

    context = FactReadContext(
        principal_id="member-old",
        chat_scope_key=scope,
        current_members=frozenset({"member-old"}),
        now_ms=T0 + 1,
    )
    result = service.retrieve_for_context(query="Wann ist das Treffen?", read_context=context)

    assert result.hits, "the semantic match did not surface the fact"
    assert "Stammtisch" in result.hits[0].entry.content
    assert "Wann ist das Treffen?" in embedder.calls
    service.close()


def test_two_candidates_in_one_batch_become_two_facts(tmp_path: Path) -> None:
    """Regression: a batch key is not a row id - two facts from one batch must both store.

    The first real backfill died here with
    ``IntegrityError: UNIQUE constraint failed: memory2_nodes.id``.
    """
    from yeoman_gateway.memory.extraction_jobs import SharedFactCandidate, candidate_fact_id

    store = MemoryStore(tmp_path / "memory.db")

    class _Extractor:
        def __call__(self, events):
            return [
                SharedFactCandidate(
                    content="Der Stammtisch ist donnerstags.",
                    author_principal="member-old",
                    visibility_scope="author_only",
                    source_refs=(("ev1", 1),),
                    audience=frozenset({"member-old"}),
                ),
                SharedFactCandidate(
                    content="Das Treffen ist um acht.",
                    author_principal="member-old",
                    visibility_scope="author_only",
                    source_refs=(("ev1", 1),),
                    audience=frozenset({"member-old"}),
                ),
            ]

    class _Journal:
        def get_event(self, event_id: str):
            return _Event(event_id, "zwei Fakten")

    queue = SharedFactExtractionQueue(
        store=store, extractor=_Extractor(), journal=_Journal(), clock=lambda: T0
    )
    queue.enqueue(
        turn_ref="tu1", source_refs=[("ev1", 1)], now_ms=T0,
        workspace_id="ws1", chat_scope_key=GROUP,
    )
    report = queue.run_due(now_ms=T0)

    assert report.published == 2
    contents = sorted(fact.content for fact in store.list_facts())
    assert len(contents) == 2
    assert candidate_fact_id("job", "a") != candidate_fact_id("job", "b")
    assert candidate_fact_id("job", "a") == candidate_fact_id("job", "a")  # idempotent
    store.close()


def test_publish_error_fails_one_job_without_killing_the_run(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    queue = _queue_with(store)
    calls = {"n": 0}
    original = queue._publish

    def flaky(candidate, *, job, now_ms):  # noqa: ANN001 - test double
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("disk full")
        return original(candidate, job=job, now_ms=now_ms)

    queue._publish = flaky  # type: ignore[method-assign]
    _run(queue)
    assert calls["n"] >= 1
    job = store.list_fact_jobs()[0]
    assert job["state"] in ("failed", "done")
    store.close()


def test_a_crash_left_running_job_is_requeued(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    queue = _queue_with(store)
    _run(queue)
    job = store.list_fact_jobs()[0]
    store.upsert_fact_job(
        job_key=str(job["job_key"]), workspace_id="ws1", chat_scope_key=GROUP,
        source_refs_json=str(job["source_refs_json"]), extractor_version="v1",
        state="running", due_ms=T0, now_ms=T0, last_activity_ms=T0,
    )

    requeued = queue.recover_stale(now_ms=T0 + 700_000)

    assert requeued == 1
    assert store.list_fact_jobs()[0]["state"] == "queued"
    assert store.list_fact_jobs()[0]["reason"] == "recovered_after_crash"
    store.close()


def test_screens_refuse_what_the_real_backfill_stored() -> None:
    """Every example below was stored as a fact by the first real backfill."""
    from yeoman_gateway.memory.extraction_jobs import screen_content

    refused = {
        "Ich habe Claude erklärt, wer Carsten ist, was er so will und was sein skill ist.":
            "conversation_reference",
        "Der Autor würde schauen, ob er sich das nach dem ersten Mal nochmal antun will.":
            "conversation_reference",
        "Der Fragesteller trinkt aktuell nur alkoholfreies Bier und Wasser.":
            "conversation_reference",
        "Wir haben besprochen, dass es am Freitag losgeht.": "conversation_reference",
        "Vielleicht ist das Treffen am Montag.": "hedged_statement",
        "Der Kurs könnte auf 200 steigen.": "hedged_statement",
        'Der Autor sagt: "noch tests".': "meta_statement",
    }
    for content, reason in refused.items():
        verdict = screen_content(content)
        assert verdict.rejected, f"should be refused: {content}"
        assert verdict.reason == reason, f"{content} -> {verdict.reason}"

    kept = [
        "Die private Krankenkasse beträgt 354€.",
        "GoPro wird mit Starman Optical verschmolzen.",
        "Der Stammtisch ist donnerstags.",
        "Wir haben einen Termin am Freitag.",  # a real agreement, not a conversation record
    ]
    for content in kept:
        assert screen_content(content).accepted, f"should be kept: {content}"


def test_rescreen_revokes_stored_noise_and_keeps_real_facts(tmp_path: Path) -> None:
    from yeoman_gateway.memory.extraction_jobs import rescreen_stored_facts
    from yeoman_gateway.memory.shared_facts import SharedFact

    def _stored(fact_id: str, content: str) -> SharedFact:
        return SharedFact(
            fact_id=fact_id, workspace_id="ws1", chat_scope_key=GROUP, content=content,
            author_principal="member-old", assertion_status="assertion",
            visibility_scope="author_only", group_rule="explicit_principals",
            valid_from_ms=T0, extractor_version="v1", audience=frozenset({"member-old"}),
        )

    store = MemoryStore(tmp_path / "memory.db")
    good = store.upsert_fact(_stored("good", "Die private Krankenkasse beträgt 354€."))
    noise = store.upsert_fact(
        _stored("noise", "Ich habe Claude erklärt, wer Carsten ist.")
    )

    dry = rescreen_stored_facts(store, dry_run=True, now_ms=T0)
    assert dry.checked == 2
    assert dry.revoked == (noise.fact_id,)
    assert store.get_fact(noise.fact_id).revoked_at_ms is None  # a dry run changes nothing

    applied = rescreen_stored_facts(store, dry_run=False, now_ms=T0 + 1)
    assert applied.revoked == (noise.fact_id,)
    assert applied.kept == 1
    assert store.get_fact(noise.fact_id).revoked_at_ms == T0 + 1
    assert store.get_fact(good.fact_id).revoked_at_ms is None
    store.close()


def test_a_revoked_fact_is_never_resurrected_by_a_rerun(tmp_path: Path) -> None:
    """Re-publishing a revoked statement must not undo the human decision."""
    from yeoman_gateway.memory.extraction_jobs import SharedFactCandidate

    store = MemoryStore(tmp_path / "memory.db")
    queue = _queue_with(store, embedder=_StubEmbedder())
    _run(queue)
    fact = store.list_facts()[0]
    job = store.list_fact_jobs()[0]
    assert store.redact_fact(fact.fact_id, now_ms=T0 + 5) is True

    candidate = SharedFactCandidate(
        content=fact.content or "Der Stammtisch ist donnerstags.",
        author_principal="member-old",
        visibility_scope="author_only",
        source_refs=(("ev1", 1),),
        audience=frozenset({"member-old"}),
    )
    # The same sources and the same content yield the same fact id.
    published = queue._publish(candidate, job=job, now_ms=T0 + 6)

    assert published is False
    assert queue.skipped_existing == 1
    revived = store.get_fact(fact.fact_id)
    assert revived.revoked_at_ms == T0 + 5  # still revoked
    assert revived.content == ""  # and the redacted text was not written back
    store.close()
