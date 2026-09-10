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
        _payload({"content": "Vielleicht morgen.", "basis": "vibes"}),
        members=frozenset({"member-old"}),
    )

    candidate = extractor([_Event("ev1", "Vielleicht morgen.")])[0]

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
