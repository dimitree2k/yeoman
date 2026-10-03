"""Durable knowledge evidence for Participation approvals (steps 3 and 4).

A knowledge-backed approval has to survive a process restart. The admission therefore
carries a bounded, versioned, private snapshot of exactly the knowledge a later
revalidation must find again. These tests pin the codec, the durable admission record,
the staging path and the unchanged draft/target/revision binding.

They also pin the durable revalidation itself: one validator, reading only the persisted
evidence plus the current membership and knowledge policy, guards both the owner-approval
commit and the pre-dispatch (transport) hook, and refuses closed whenever it cannot read
the authority it needs.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from yeoman_gateway.consciousness.log import SpeakupLog, deterministic_effect_id
from yeoman_gateway.knowledge._memory.shared_facts import FactReadContext, FactRetrievalResult
from yeoman_gateway.knowledge.models import (
    KnowledgeContext,
    SourceRef,
    StatementCandidate,
    TrustedCaptureContext,
    TrustedReadContext,
)
from yeoman_gateway.policy.engine import PolicyEngine
from yeoman_gateway.policy.schema import PolicyConfig
from yeoman_gateway.processing.models import (
    EffectEnvelope,
    EffectTarget,
    PolicySnapshot,
    TextPayload,
    payload_hash,
)
from yeoman_gateway.processing.participation import (
    ParticipationDecision,
    ParticipationOpportunity,
)
from yeoman_gateway.processing.participation_knowledge import (
    ParticipationKnowledgeEvidenceError,
    ParticipationKnowledgeReader,
    ParticipationKnowledgeReaders,
    ParticipationKnowledgeRecord,
    ParticipationKnowledgeSelection,
    ParticipationKnowledgeSelector,
    reader_key,
    selection_from_mapping,
    selection_to_mapping,
)
from yeoman_gateway.processing.participation_runtime import (
    ParticipationAdmission,
    ParticipationRuntime,
)
from yeoman_gateway.processing.store import ProcessingStore

from tests.gateway.capture_harness import AUTHOR, GROUP, OTHER, CaptureHarness

NOW = 1_800_000_000_000
CHANNEL = "whatsapp"
CHAT = "group-1@g.us"
OWNER = "4915112345678"
MEMBERS = frozenset({"alice", "bob"})
MARKER = "SECRET-KNOWLEDGE-MARKER"
FIXED_NOW = datetime(2026, 4, 25, 12, 0, tzinfo=UTC)
# The policy identity the dispatch harness resolves for its synthetic target.
DISPATCH_POLICY_VERSION = "snapshot:1"
DISPATCH_POLICY_HASH = "a" * 32


# -- selector harness ----------------------------------------------------------------


def _contexts(*, principal: str = "alice"):
    return (
        TrustedReadContext(
            principal_id=principal,
            channel=CHANNEL,
            chat_id=CHAT,
            recipient_principals=MEMBERS,
            membership_revision="membership-v3",
            policy_revision=8,
            purpose="proactive",
            now_ms=NOW,
        ),
        FactReadContext(
            principal_id=principal,
            chat_scope_key=f"channel:{CHANNEL}:chat:{CHAT}",
            current_members=MEMBERS,
            epoch=4,
            now_ms=NOW,
        ),
    )


@dataclass
class _Knowledge:
    result: KnowledgeContext

    def recall(self, query, **kwargs):
        del query, kwargs
        return self.result

    def revalidate(self, result, **kwargs):
        del kwargs
        return self.result


@dataclass
class _Memory:
    result: FactRetrievalResult

    def retrieve_for_context(self, **kwargs):
        del kwargs
        return self.result

    def revalidate_for_context(self, result, **kwargs):
        del kwargs
        return self.result


def _selector(*, statement_text: str = "", fact_text: str = ""):
    # The statement id is its source event id, so a selected statement also carries the
    # source revision a revalidation has to find again.
    ref = SourceRef("ev-1", 1, CHANNEL, CHAT, "alice", NOW - 7 * 86_400_000)
    knowledge = _Knowledge(
        KnowledgeContext(
            text=statement_text,
            statement_ids=("ev-1",) if statement_text else (),
            source_refs=(ref,) if statement_text else (),
            context_revision="statement-r1",
        )
    )
    memory = _Memory(
        FactRetrievalResult(
            text=f"- {fact_text}" if fact_text else "",
            hits=(
                SimpleNamespace(entry=SimpleNamespace(id="fact-1", content=fact_text)),
            )
            if fact_text
            else (),
            used_source_refs={"fact-1": (("ev-2", 1),)} if fact_text else {},
        )
    )
    return ParticipationKnowledgeSelector(knowledge=knowledge, memory=memory)


def _selected(
    *,
    statement_text: str = "",
    fact_text: str = "",
    query: str = "Wann ist der Stammtisch?",
):
    read_context, fact_context = _contexts()
    return _selector(statement_text=statement_text, fact_text=fact_text).select(
        query=query, read_context=read_context, fact_context=fact_context
    )


def _statement_selection():
    return _selected(statement_text=f"Stammtisch donnerstags. {MARKER}")


def _fact_selection():
    return _selected(fact_text="Gegessen wird um sieben.")


def _multiline_selection():
    return _selected(
        statement_text=f"Stammtisch donnerstags. {MARKER}",
        fact_text="Gegessen wird um sieben.",
        query="Wann ist der Stammtisch?\nWird davor gegessen?",
    )


class _PerReader:
    def __init__(self, per_reader, kind: str) -> None:
        self._per_reader = per_reader
        self._kind = kind

    def _target(self, context):
        target = self._per_reader[str(context.principal_id)]
        return target[0] if self._kind == "knowledge" else target[1]

    def recall(self, query, **kwargs):
        return self._target(kwargs["context"])

    def revalidate(self, result, **kwargs):
        return self._target(kwargs["context"])

    def retrieve_for_context(self, **kwargs):
        return self._target(kwargs["read_context"])

    def revalidate_for_context(self, result, **kwargs):
        return self._target(kwargs["read_context"])


def _multi_author_selection():
    def knowledge(text: str, statement_id: str) -> KnowledgeContext:
        return KnowledgeContext(
            text=text,
            statement_ids=(statement_id,) if text else (),
            source_refs=(
                SourceRef(statement_id, 1, CHANNEL, CHAT, "alice", NOW - 86_400_000),
            )
            if text
            else (),
            context_revision=f"{statement_id}-r1",
        )

    def memory(text: str) -> FactRetrievalResult:
        return FactRetrievalResult(
            text=f"- {text}" if text else "",
            hits=(
                SimpleNamespace(entry=SimpleNamespace(id="fact-shared", content=text)),
            )
            if text
            else (),
            used_source_refs={"fact-shared": (("ev-shared", 1),)} if text else {},
        )

    shared = f"Both hear this. {MARKER}"
    per_reader = {
        "alice": (knowledge("Alice only hears this.", "stmt-alice"), memory(shared)),
        "bob": (knowledge("Bob only hears this.", "stmt-bob"), memory(shared)),
    }
    selector = ParticipationKnowledgeSelector(
        knowledge=_PerReader(per_reader, "knowledge"), memory=_PerReader(per_reader, "memory")
    )
    readers = ParticipationKnowledgeReaders(
        readers=tuple(
            ParticipationKnowledgeReader(*_contexts(principal=principal))
            for principal in ("alice", "bob")
        )
    )
    return selector.select_for_readers(query="topic", readers=readers)


# -- A. codec round trips ------------------------------------------------------------


def test_codec_round_trips_a_statements_only_selection() -> None:
    selection = _statement_selection()
    assert selection.text and not selection.facts.used_source_refs
    assert selection.records[0].refs == (("ev-1", 1),)
    assert selection.reader_keys == (reader_key(*_contexts()),)

    restored = selection_from_mapping(selection_to_mapping(selection))

    assert restored.text == selection.text
    assert restored.records == selection.records
    assert restored.reader_keys == selection.reader_keys
    assert restored.query == selection.query
    assert restored.revision == selection.revision
    assert restored.reason == selection.reason
    assert restored.statements.statement_ids == selection.statements.statement_ids
    assert restored.statements.text == selection.statements.text
    assert tuple(ref.key for ref in restored.statements.source_refs) == tuple(
        ref.key for ref in selection.statements.source_refs
    )
    assert selection_to_mapping(restored) == selection_to_mapping(selection)


def test_codec_round_trips_a_shared_facts_only_selection() -> None:
    selection = _fact_selection()
    assert selection.text == "- Gegessen wird um sieben."
    assert not selection.statements.statement_ids

    restored = selection_from_mapping(selection_to_mapping(selection))

    assert restored.text == selection.text
    assert restored.records == selection.records
    assert restored.reader_keys == selection.reader_keys
    assert restored.revision == selection.revision
    assert restored.facts.text == selection.facts.text
    assert dict(restored.facts.used_source_refs) == dict(selection.facts.used_source_refs)
    assert restored.statements.statement_ids == ()


def test_codec_round_trips_multiline_text_and_query() -> None:
    selection = _multiline_selection()
    assert len(selection.text.splitlines()) == 2
    # The query is bounded and normalized to one line before it is ever rendered.
    assert selection.query == "Wann ist der Stammtisch? Wird davor gegessen?"

    restored = selection_from_mapping(selection_to_mapping(selection))

    assert restored.text == selection.text
    assert restored.query == selection.query
    assert restored.records == selection.records
    assert restored.statements.text == "\n".join(
        record.text for record in selection.records if record.kind == "statement"
    )
    assert restored.facts.text == "\n".join(
        record.text for record in selection.records if record.kind == "fact"
    )


def test_codec_preserves_a_multiline_query_verbatim() -> None:
    selection = ParticipationKnowledgeSelection(
        text="- Zeile eins\n- Zeile zwei",
        revision="rev-multi",
        reason="selected",
        records=(
            ParticipationKnowledgeRecord(
                kind="fact", record_id="fact-1", text="- Zeile eins", refs=(("ev-1", 1),)
            ),
        ),
        reader_keys=(
            (
                "alice",
                CHANNEL,
                CHAT,
                "membership-v3",
                f"channel:{CHANNEL}:chat:{CHAT}",
                "proactive",
                8,
            ),
        ),
        query="Zeile eins?\nZeile zwei?",
        recipient_principals=("alice", "bob"),
        current_members=("alice", "bob"),
        now_ms=NOW,
    )

    restored = selection_from_mapping(selection_to_mapping(selection))

    assert restored.query == selection.query
    assert restored.text == selection.text
    assert restored.records == selection.records
    assert restored.reader_keys == selection.reader_keys


def test_codec_round_trips_multi_author_reader_identities() -> None:
    selection = _multi_author_selection()
    assert len(selection.reader_keys) == 2
    assert all(isinstance(key, tuple) and len(key) == 7 for key in selection.reader_keys)

    mapping = selection_to_mapping(selection)
    restored = selection_from_mapping(mapping)

    assert restored.reader_keys == selection.reader_keys
    assert restored.records == selection.records
    assert restored.text == selection.text
    assert restored.revision == selection.revision
    assert [list(key) for key in restored.reader_keys] == [
        list(key) for key in selection.reader_keys
    ]
    assert selection_to_mapping(restored) == mapping


def test_codec_mapping_is_json_native_bounded_and_deterministic() -> None:
    selection = _multiline_selection()
    mapping = selection_to_mapping(selection)

    assert json.loads(json.dumps(mapping)) == mapping
    assert mapping == selection_to_mapping(selection)
    assert set(mapping) == {
        "version",
        "text",
        "query",
        "revision",
        "reason",
        "records",
        "readers",
        "reader_prerequisites",
    }
    assert mapping["version"] == 1
    assert set(mapping["reader_prerequisites"]) == {
        "recipient_principals",
        "current_members",
        "now_ms",
        "is_direct",
    }
    assert mapping["reader_prerequisites"]["recipient_principals"] == ["alice", "bob"]
    assert mapping["reader_prerequisites"]["current_members"] == ["alice", "bob"]
    assert mapping["reader_prerequisites"]["is_direct"] is False
    for record in mapping["records"]:
        assert set(record) == {"kind", "record_id", "text", "refs"}
        assert record["kind"] in {"statement", "fact"}
        assert all(len(ref) == 2 for ref in record["refs"])


def test_codec_never_carries_transcripts_messages_or_model_output() -> None:
    selection = _multiline_selection()
    mapping = selection_to_mapping(selection)
    serialized = json.dumps(mapping)

    forbidden = ("messages", "transcript", "prompt", "payload", "reply_to", "model")
    for name in forbidden:
        assert f'"{name}"' not in serialized
    # The only free text is the bounded rendered selection and its own identifiers.
    assert len(mapping["text"]) <= 1200
    assert len(mapping["query"]) <= 600
    assert len(mapping["records"]) <= 3
    assert len(mapping["readers"]) <= 16


# -- A. codec rejection --------------------------------------------------------------

_REQUIRED_KEYS = (
    "version",
    "text",
    "query",
    "revision",
    "reason",
    "records",
    "readers",
    "reader_prerequisites",
)


def _valid_evidence() -> dict[str, Any]:
    return selection_to_mapping(_multiline_selection())


@pytest.mark.parametrize("key", _REQUIRED_KEYS)
def test_codec_rejects_a_missing_required_key(key: str) -> None:
    mapping = _valid_evidence()
    del mapping[key]

    with pytest.raises(ParticipationKnowledgeEvidenceError):
        selection_from_mapping(mapping)


_MALFORMED: list[tuple[str, Any]] = [
    ("unsupported_version", lambda m: m.__setitem__("version", 2)),
    ("string_version", lambda m: m.__setitem__("version", "1")),
    ("bool_version", lambda m: m.__setitem__("version", True)),
    ("int_text", lambda m: m.__setitem__("text", 5)),
    ("oversized_text", lambda m: m.__setitem__("text", "x" * 1201)),
    ("oversized_query", lambda m: m.__setitem__("query", "x" * 601)),
    ("non_string_query", lambda m: m.__setitem__("query", None)),
    ("non_string_revision", lambda m: m.__setitem__("revision", 1)),
    ("oversized_reason", lambda m: m.__setitem__("reason", "r" * 129)),
    ("unknown_top_level_key", lambda m: m.__setitem__("transcript", [])),
    ("records_not_a_list", lambda m: m.__setitem__("records", {})),
    ("too_many_records", lambda m: m.__setitem__("records", m["records"] * 4)),
    ("record_not_an_object", lambda m: m["records"].__setitem__(0, "statement")),
    ("unknown_record_key", lambda m: m["records"][0].__setitem__("prompt", "x")),
    ("unknown_record_kind", lambda m: m["records"][0].__setitem__("kind", "message")),
    ("empty_record_id", lambda m: m["records"][0].__setitem__("record_id", "")),
    ("int_record_id", lambda m: m["records"][0].__setitem__("record_id", 7)),
    ("empty_record_text", lambda m: m["records"][0].__setitem__("text", "")),
    ("oversized_record_text", lambda m: m["records"][0].__setitem__("text", "x" * 1201)),
    ("refs_not_a_list", lambda m: m["records"][0].__setitem__("refs", "ev-1")),
    ("ref_arity", lambda m: m["records"][0].__setitem__("refs", [["ev-1"]])),
    ("empty_ref_event_id", lambda m: m["records"][0].__setitem__("refs", [["", 1]])),
    ("zero_ref_revision", lambda m: m["records"][0].__setitem__("refs", [["ev-1", 0]])),
    ("string_ref_revision", lambda m: m["records"][0].__setitem__("refs", [["ev-1", "1"]])),
    ("bool_ref_revision", lambda m: m["records"][0].__setitem__("refs", [["ev-1", True]])),
    ("readers_not_a_list", lambda m: m.__setitem__("readers", "reader")),
    ("too_many_readers", lambda m: m.__setitem__("readers", m["readers"] * 17)),
    ("reader_arity_short", lambda m: m["readers"][0].__setitem__(slice(None), m["readers"][0][:6])),
    ("reader_arity_long", lambda m: m["readers"][0].append("proactive")),
    ("reader_non_string", lambda m: m["readers"][0].__setitem__(0, 5)),
    ("reader_empty_channel", lambda m: m["readers"][0].__setitem__(1, "")),
    ("reader_policy_revision_string", lambda m: m["readers"][0].__setitem__(6, "8")),
    ("reader_policy_revision_bool", lambda m: m["readers"][0].__setitem__(6, True)),
    ("reader_policy_revision_negative", lambda m: m["readers"][0].__setitem__(6, -1)),
    ("prerequisites_not_an_object", lambda m: m.__setitem__("reader_prerequisites", [])),
    ("prerequisites_missing_key", lambda m: m["reader_prerequisites"].pop("now_ms")),
    ("prerequisites_unknown_key", lambda m: m["reader_prerequisites"].__setitem__("epoch", 4)),
    ("prerequisites_bad_principal", lambda m: m["reader_prerequisites"][
        "recipient_principals"
    ].__setitem__(0, 5)),
    (
        "prerequisites_mismatched_sets",
        lambda m: m["reader_prerequisites"].__setitem__("current_members", ["alice", "carol"]),
    ),
    (
        "prerequisites_empty_with_readers",
        lambda m: m["reader_prerequisites"].__setitem__("recipient_principals", []),
    ),
    ("prerequisites_negative_now", lambda m: m["reader_prerequisites"].__setitem__("now_ms", -1)),
    ("prerequisites_string_now", lambda m: m["reader_prerequisites"].__setitem__("now_ms", "0")),
    ("prerequisites_bool_now", lambda m: m["reader_prerequisites"].__setitem__("now_ms", True)),
    ("prerequisites_bad_is_direct", lambda m: m["reader_prerequisites"].__setitem__(
        "is_direct", "no"
    )),
    ("records_without_readers", lambda m: m.__setitem__("readers", [])),
]


@pytest.mark.parametrize(
    "mutate", [mutate for _, mutate in _MALFORMED], ids=[name for name, _ in _MALFORMED]
)
def test_codec_rejects_malformed_evidence(mutate) -> None:
    mapping = _valid_evidence()
    mutate(mapping)

    with pytest.raises(ParticipationKnowledgeEvidenceError):
        selection_from_mapping(mapping)


@pytest.mark.parametrize("value", [None, [], "evidence", 7])
def test_codec_rejects_a_non_mapping(value: object) -> None:
    with pytest.raises(ParticipationKnowledgeEvidenceError):
        selection_from_mapping(value)  # type: ignore[arg-type]


def test_codec_rejects_an_unreadable_selection_before_it_is_persisted() -> None:
    oversized = _selected(statement_text="x" * 1201)

    with pytest.raises(ParticipationKnowledgeEvidenceError):
        selection_to_mapping(oversized)


# -- B. durable persistence ----------------------------------------------------------


def _admission(
    *,
    payload_hash: str,
    knowledge_evidence: dict[str, Any] | None = None,
    channel: str = CHANNEL,
    chat_id: str = CHAT,
    source_event_ids: tuple[str, ...] = ("m1",),
    source_principals: tuple[tuple[str, str], ...] = (("m1", "alice"),),
    policy_version: str = DISPATCH_POLICY_VERSION,
    policy_hash: str = DISPATCH_POLICY_HASH,
    arbitration_revision: int = 1,
    approval_revision: int = 1,
) -> ParticipationAdmission:
    return ParticipationAdmission(
        opportunity_id="opp-1",
        channel=channel,
        chat_id=chat_id,
        activation_epoch=1,
        lane="production",
        observed_revision=3,
        action="comment",
        intent="initiate",
        purpose="synthetic",
        admission_id="adm-1",
        source_event_ids=source_event_ids,
        source_principals=source_principals,
        policy_version=policy_version,
        policy_hash=policy_hash,
        arbitration_revision=arbitration_revision,
        contribution_type="observation",
        payload_hash=payload_hash,
        approval_revision=approval_revision,
        knowledge_evidence=knowledge_evidence,
    )


def test_admission_evidence_defaults_to_none() -> None:
    assert _admission(payload_hash="x").knowledge_evidence is None


def _envelope(
    effect_id: str = "effect-evidence-1",
    text: str = "prepared draft",
    *,
    channel: str = CHANNEL,
    chat_id: str = CHAT,
) -> EffectEnvelope:
    return EffectEnvelope(
        effect_id=effect_id,
        operation_key=f"participation:{effect_id}",
        payload=TextPayload(text=text),
        target=EffectTarget(channel=channel, chat_id=chat_id),
        principal="service:speakup",
        capability="send_text",
        origin="participation",
        admission_id="adm-1",
    )


def test_persisted_admission_keeps_evidence_across_a_store_restart(tmp_path: Path) -> None:
    evidence = selection_to_mapping(_statement_selection())
    envelope = _envelope()
    store = ProcessingStore(tmp_path / "processing.db")
    try:
        store.enqueue_participation_effect(
            envelope,
            _admission(payload_hash=envelope.payload_hash, knowledge_evidence=evidence),
        )
    finally:
        store.close()

    reopened = ProcessingStore(tmp_path / "processing.db")
    try:
        restored = reopened.get_participation_admission("adm-1")
    finally:
        reopened.close()

    assert restored is not None
    assert restored.knowledge_evidence == evidence
    assert restored.admission_id == "adm-1"
    assert restored.payload_hash == envelope.payload_hash
    assert restored.payload_hash == payload_hash(TextPayload(text="prepared draft"))
    decoded = selection_from_mapping(restored.knowledge_evidence or {})
    assert decoded.text == _statement_selection().text
    assert decoded.records == _statement_selection().records
    assert decoded.reader_keys == _statement_selection().reader_keys


def test_persisted_admission_without_evidence_still_decodes(tmp_path: Path) -> None:
    envelope = _envelope("effect-legacy-1")
    store = ProcessingStore(tmp_path / "processing.db")
    try:
        store.enqueue_participation_effect(
            envelope, _admission(payload_hash=envelope.payload_hash)
        )
        restored = store.get_participation_admission("adm-1")
    finally:
        store.close()

    assert restored is not None
    assert restored.knowledge_evidence is None
    assert restored.payload_hash == envelope.payload_hash


def test_admission_evidence_is_bound_to_the_admission_record(tmp_path: Path) -> None:
    from yeoman_gateway.processing.models import EffectConflictError

    first = selection_to_mapping(_statement_selection())
    changed = dict(first, text="A different approved draft basis.")
    store = ProcessingStore(tmp_path / "processing.db")
    try:
        envelope = _envelope()
        store.enqueue_participation_effect(
            envelope, _admission(payload_hash=envelope.payload_hash, knowledge_evidence=first)
        )
        with pytest.raises(EffectConflictError):
            store.enqueue_participation_effect(
                _envelope("effect-evidence-2"),
                _admission(payload_hash=envelope.payload_hash, knowledge_evidence=changed),
            )
        restored = store.get_participation_admission("adm-1")
    finally:
        store.close()

    assert restored is not None
    assert restored.knowledge_evidence == first


# -- B. staging path -----------------------------------------------------------------


class _AllowSecurity:
    def check_output(self, text: str, context: dict[str, object] | None = None):
        del text, context
        from yeoman_gateway.core.models import SecurityDecision, SecurityResult

        return SecurityResult(
            stage="output", decision=SecurityDecision(action="allow", reason="ok")
        )


class _RecordingEffects:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def send(self, **kwargs: object):
        self.calls.append(dict(kwargs))
        return SimpleNamespace(accepted=True)


@dataclass
class _Submission:
    calls: list[dict[str, object]] = field(default_factory=list)

    async def submit(self, *, admission, effect_id, content, payload_hash):
        self.calls.append(
            {
                "admission": admission,
                "effect_id": effect_id,
                "content": content,
                "payload_hash": payload_hash,
            }
        )
        return SimpleNamespace(status="submitted")


def _approval_chat() -> dict[str, object]:
    """One chat whose participation previews to the owner before it may send."""
    return {
        "whoCanTalk": {"mode": "everyone"},
        "spontaneity": {
            "enabled": True,
            "profile": "balanced",
            "preview": "owner_dm",
        },
    }


def _policy() -> object:
    return PolicyConfig.model_validate(
        {
            "owners": {CHANNEL: [OWNER]},
            "channels": {
                CHANNEL: {
                    "chats": {
                        CHAT: _approval_chat(),
                        # The real-store harness captures in this chat, so a proposal there
                        # can be revalidated against the same membership.
                        GROUP: _approval_chat(),
                    }
                }
            },
        }
    )


def _build_tools(tmp_path: Path):
    from yeoman_gateway.bus.queue import MessageBus
    from yeoman_gateway.consciousness.approval import SpeakupApprovalStore
    from yeoman_gateway.consciousness.tools import ConsciousnessTools
    from yeoman_gateway.storage.inbound_archive import InboundArchive
    from yeoman_shared.config.schema import Config, ConsciousnessConfig

    config = Config(
        consciousness=ConsciousnessConfig.model_validate(
            {
                "enabled": True,
                "ownerDmDefaultEnabled": False,
                "defaultDailyCap": 3,
                "approvalTimeoutSeconds": 3600,
                "maxSpeakupLengthChars": 400,
            }
        )
    )
    log = SpeakupLog(tmp_path / "speakups.db")
    archive = InboundArchive(tmp_path / "inbound.db")
    effects = _RecordingEffects()
    tools = ConsciousnessTools(
        config=config,
        policy_engine=PolicyEngine(_policy(), workspace=tmp_path),
        bus=MessageBus(),
        log=log,
        inbound_archive=archive,
        memory=None,
        security=_AllowSecurity(),
        approval_store=SpeakupApprovalStore(
            tmp_path / "approvals.json", now=lambda: FIXED_NOW.timestamp()
        ),
        service_effects=effects,
        now=lambda: FIXED_NOW,
    )
    tools.begin_run(trigger="cron")
    return tools, log, archive, effects


def _opportunity(chat_id: str = CHAT) -> ParticipationOpportunity:
    return ParticipationOpportunity(
        opportunity_id="opp-1",
        channel=CHANNEL,
        chat_id=chat_id,
        trigger="inbound",
        source_event_ids=("m1",),
        observed_revision=3,
        activation_epoch=1,
        created_at_ms=NOW,
    )


def _decision() -> ParticipationDecision:
    return ParticipationDecision(
        action="comment",
        intent="initiate",
        reason="synthetic",
        purpose="synthetic",
        contribution_type="observation",
    )


def _effect_id(chat_id: str = CHAT) -> str:
    return deterministic_effect_id(
        channel=CHANNEL, chat_id=chat_id, operation="comment", proposal_id="opp-1"
    )


async def _stage(
    tools, *, knowledge_evidence: dict[str, Any] | None = None, chat: str = CHAT, **kwargs
):
    return await tools.stage_participation_approval(
        opportunity=_opportunity(chat),
        decision=_decision(),
        admission=_admission(payload_hash="", chat_id=chat),
        effect_id=_effect_id(chat),
        content="the prepared draft",
        snapshot={},
        knowledge_evidence=knowledge_evidence,
        **kwargs,
    )


async def _stored_snapshot(log) -> dict[str, Any]:
    row = await log.proposal_row("opp-1")
    assert row is not None
    return json.loads(str(row["context_snapshot_json"]))


async def _claim(log, snapshot: dict[str, Any]) -> None:
    assert await log.record_approval_claim(
        "opp-1",
        owner_channel=str(snapshot["approval_owner_channel"]),
        owner_chat_id=str(snapshot["approval_owner_chat_id"]),
        owner_id=OWNER,
        payload_hash=str(snapshot["payload_hash"]),
        proposal_revision=int(snapshot["proposal_revision"]),
        target_effect_id=str(snapshot["effect_id"]),
        now_ms=1_000,
    )


async def test_staging_persists_the_evidence_and_reconstructs_it_after_a_restart(
    tmp_path: Path,
) -> None:
    evidence = selection_to_mapping(_statement_selection())
    tools, log, archive, effects = _build_tools(tmp_path)

    result = await _stage(tools, knowledge_evidence=evidence)
    assert result["status"] == "awaiting_approval"

    snapshot = await _stored_snapshot(log)
    stored = snapshot["participation_admission"]
    assert stored["knowledge_evidence"] == evidence
    # The evidence never replaces or rewrites the text-payload hash.
    assert stored["payload_hash"] == payload_hash(TextPayload(text="the prepared draft"))
    # The private evidence is never part of the owner preview.
    assert effects.calls
    assert all(MARKER not in str(call["content"]) for call in effects.calls)

    await _claim(log, snapshot)
    log.close()
    archive.close()

    restarted, restarted_log, restarted_archive, _ = _build_tools(tmp_path)
    submission = _Submission()
    restarted.set_participation_submission(submission)
    try:
        outcome = await restarted.submit_proposal("opp-1")
    finally:
        restarted_log.close()
        restarted_archive.close()

    assert outcome["status"] == "submitted"
    assert len(submission.calls) == 1
    admission = submission.calls[0]["admission"]
    assert isinstance(admission, ParticipationAdmission)
    assert admission.knowledge_evidence == evidence
    decoded = selection_from_mapping(admission.knowledge_evidence)
    assert decoded.text == _statement_selection().text
    assert decoded.records == _statement_selection().records
    assert decoded.revision == _statement_selection().revision


async def test_staging_a_legacy_recent_only_proposal_still_works(tmp_path: Path) -> None:
    tools, log, archive, _effects = _build_tools(tmp_path)

    result = await _stage(tools)
    assert result["status"] == "awaiting_approval"

    snapshot = await _stored_snapshot(log)
    assert snapshot["participation_admission"]["knowledge_evidence"] is None

    await _claim(log, snapshot)
    log.close()
    archive.close()

    restarted, restarted_log, restarted_archive, _ = _build_tools(tmp_path)
    submission = _Submission()
    restarted.set_participation_submission(submission)
    try:
        outcome = await restarted.submit_proposal("opp-1")
    finally:
        restarted_log.close()
        restarted_archive.close()

    assert outcome["status"] == "submitted"
    assert len(submission.calls) == 1
    admission = submission.calls[0]["admission"]
    assert isinstance(admission, ParticipationAdmission)
    assert admission.knowledge_evidence is None


async def test_queue_approval_adapter_forwards_the_evidence() -> None:
    from yeoman_gateway.app.bootstrap import _ParticipationSubmission

    class _Queue:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def stage_participation_approval(self, **kwargs):
            self.calls.append(kwargs)
            return {"status": "awaiting_approval"}

    queue = _Queue()
    submission = _ParticipationSubmission(
        responder=object(), writer_profile="balanced", approval_tools=queue
    )
    evidence = selection_to_mapping(_statement_selection())

    forwarded = await submission.queue_approval(
        opportunity=_opportunity(),
        decision=_decision(),
        admission=_admission(payload_hash=""),
        effect_id=_effect_id(),
        content="the prepared draft",
        snapshot={},
        knowledge_evidence=evidence,
    )
    legacy = await submission.queue_approval(
        opportunity=_opportunity(),
        decision=_decision(),
        admission=_admission(payload_hash=""),
        effect_id=_effect_id(),
        content="the prepared draft",
        snapshot={},
    )

    assert forwarded == legacy == {"status": "awaiting_approval"}
    assert queue.calls[0]["knowledge_evidence"] == evidence
    assert queue.calls[1]["knowledge_evidence"] is None


# -- C. binding ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ("message", "approval_admission_changed"),
        ("target", "approval_admission_changed"),
        ("revision", "approval_binding_changed"),
    ],
)
async def test_changed_draft_target_or_revision_cannot_retain_the_approval(
    tmp_path: Path, mutation: str, expected: str
) -> None:
    evidence = selection_to_mapping(_statement_selection())
    tools, log, archive, _effects = _build_tools(tmp_path)
    try:
        assert (await _stage(tools, knowledge_evidence=evidence))["status"] == "awaiting_approval"
        snapshot = await _stored_snapshot(log)
        await _claim(log, snapshot)
        proposal = await tools._load_proposal("opp-1")
        assert proposal is not None

        if mutation == "message":
            tools._proposals["opp-1"] = replace(proposal, message="a different draft")
        elif mutation == "target":
            tools._proposals["opp-1"] = replace(proposal, chat_id="other@g.us")
        else:
            tools._proposals["opp-1"] = replace(
                proposal, proposal_revision=proposal.proposal_revision + 1
            )

        submission = _Submission()
        tools.set_participation_submission(submission)
        outcome = await tools.submit_proposal("opp-1")

        assert outcome["status"] == "rejected"
        assert outcome["reason"] == expected
        assert submission.calls == []
        stored = (await _stored_snapshot(log))["participation_admission"]
        assert stored["knowledge_evidence"] == evidence
    finally:
        log.close()
        archive.close()


# -- D. durable revalidation at both boundaries (step 4) ------------------------------

REAL_TEXT = "The shared Stammtisch note."
REAL_KEY = "3EB0R01"
REAL_AUDIENCE = frozenset({f"{AUTHOR}@s.whatsapp.net", f"{OTHER}@s.whatsapp.net"})


def _capture_shared_statement(
    harness: CaptureHarness, *, text: str = REAL_TEXT, key: str = REAL_KEY
):
    """Capture one real statement whose proven audience is the whole group."""
    event_id = harness.observe(text, message_id=key)
    source = harness.authority.verify_source_ref(event_id, 1)
    assert source is not None
    return harness.knowledge.capture(
        StatementCandidate(
            content=text,
            sources=(source,),
            extractor_version="approval-revalidation-v1",
            confidence=0.9,
        ),
        context=TrustedCaptureContext(
            request_id=f"approval-{key}",
            policy_revision=1,
            capture_basis="historic_row",
            authorized_sources=(source,),
        ),
    )


def _real_readers(harness: CaptureHarness) -> ParticipationKnowledgeReaders:
    """The two verified trigger authors, over the real store's chat and membership."""
    return ParticipationKnowledgeReaders(
        readers=tuple(
            ParticipationKnowledgeReader(
                TrustedReadContext(
                    principal_id=principal,
                    channel=CHANNEL,
                    chat_id=GROUP,
                    recipient_principals=REAL_AUDIENCE,
                    membership_revision="captured-members-v1",
                    policy_revision=1,
                    purpose="proactive",
                    now_ms=harness.now,
                    is_direct=False,
                ),
                FactReadContext(
                    principal_id=principal,
                    chat_scope_key=f"channel:{CHANNEL}:chat:{GROUP}",
                    current_members=REAL_AUDIENCE,
                    epoch=harness.memory.store.acl_epoch(),
                    now_ms=harness.now,
                ),
            )
            for principal in sorted(REAL_AUDIENCE)
        )
    )


def _real_selector(harness: CaptureHarness) -> ParticipationKnowledgeSelector:
    return ParticipationKnowledgeSelector(knowledge=harness.knowledge, memory=harness.memory)


def _real_evidence(harness: CaptureHarness) -> dict[str, Any]:
    """The durable evidence a real selection over the real store would persist."""
    selection = _real_selector(harness).select_for_readers(
        query="Stammtisch", readers=_real_readers(harness)
    )
    assert selection.text and selection.records
    return selection_to_mapping(selection)


def _real_validator(harness: CaptureHarness):
    """The production validator, composed exactly as bootstrap composes it."""
    from yeoman_gateway.app.bootstrap import _knowledge_decision_validator

    return _knowledge_decision_validator(
        chat_registry=harness.registry,
        knowledge=harness.knowledge,
        memory=harness.memory,
    )


class _RevisionKnowledge:
    """The harness knowledge with an explicit policy revision, for drift cases."""

    def __init__(self, inner: object, revision: object) -> None:
        self._inner = inner
        self.policy_revision = revision

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


def _custom_validator(*, selector: object, chat_registry: object, knowledge: object):
    """A validator over an explicit knowledge double, so the policy revision can vary."""
    from yeoman_gateway.app.bootstrap import _KnowledgeDecisionValidator

    return _KnowledgeDecisionValidator(
        selector=selector, chat_registry=chat_registry, knowledge=knowledge
    )


def _real_admission(evidence: Any) -> ParticipationAdmission:
    """An admission for the harness chat, carrying exactly the evidence under test."""
    return _admission(
        payload_hash="x",
        channel=CHANNEL,
        chat_id=GROUP,
        source_event_ids=("m1",),
        arbitration_revision=0,
        knowledge_evidence=evidence,
    )


class _UnreadableEvidence(dict):
    """A mapping whose own reads fail: unreadable authority, not merely stale content."""

    def get(self, key, default=None):
        del default
        raise RuntimeError(f"unreadable evidence: {key}")


class _BrokenRegistry:
    def get_chat(self, channel: str, chat_id: str):
        raise RuntimeError(f"registry unavailable for {channel}:{chat_id}")


class _BrokenKnowledge:
    """A knowledge store that fails every re-read; an outage must never approve."""

    def recall(self, query, **kwargs):
        del query, kwargs
        raise RuntimeError("knowledge unavailable")

    def revalidate(self, result, **kwargs):
        del result, kwargs
        raise RuntimeError("knowledge unavailable")


def _tampered(evidence: dict[str, Any], *, mutate) -> dict[str, Any]:
    """A copy of the evidence with one record field rewritten, as a corrupted store would."""
    changed = dict(evidence)
    changed["records"] = [dict(record) for record in evidence["records"]]
    mutate(changed)
    return changed


# Each case starts from evidence the validator accepts, applies one real change, and
# pins which of the five decisions the validator actually returns for it.
def _case_unchanged(harness, evidence, admission):
    return _real_validator(harness), admission


def _case_legacy_without_evidence(harness, evidence, admission):
    del evidence
    return _real_validator(harness), replace(admission, knowledge_evidence=None)


def _case_statement_revoked(harness, evidence, admission):
    assert harness.delete(REAL_KEY) == (REAL_KEY,)
    assert any(row["status"] == "revoked" for row in harness.statements())
    return _real_validator(harness), admission


def _case_suppressed_text_with_live_records(harness, evidence, admission):
    """Suppressing only the rendered text must not skip the store re-read.

    A snapshot with records but empty text used to short-circuit revalidation to
    "unchanged", which would have approved a draft whose knowledge had since been
    revoked. The records still claim the draft used knowledge, so the read must happen
    and the difference must invalidate the approval.
    """
    assert harness.delete(REAL_KEY) == (REAL_KEY,)
    blanked = dict(evidence, text="")
    assert blanked["records"], "the case needs records to be meaningful"
    return _real_validator(harness), replace(admission, knowledge_evidence=blanked)


def _case_rendered_text_changed(harness, evidence, admission):
    changed = dict(evidence, text=evidence["text"] + " (restated elsewhere)")
    return _real_validator(harness), replace(admission, knowledge_evidence=changed)


def _case_record_text_changed(harness, evidence, admission):
    changed = _tampered(
        evidence,
        mutate=lambda item: item["records"][0].__setitem__(
            "text", "A different rendering of the same statement."
        ),
    )
    return _real_validator(harness), replace(admission, knowledge_evidence=changed)


def _case_source_revision_changed(harness, evidence, admission):
    def mutate(item):
        refs = item["records"][0]["refs"]
        item["records"][0]["refs"] = [[refs[0][0], int(refs[0][1]) + 1], *refs[1:]]

    changed = _tampered(evidence, mutate=mutate)
    return _real_validator(harness), replace(admission, knowledge_evidence=changed)


def _case_source_event_unknown(harness, evidence, admission):
    def mutate(item):
        refs = item["records"][0]["refs"]
        item["records"][0]["refs"] = [["wa_unknown_source_event", refs[0][1]], *refs[1:]]

    changed = _tampered(evidence, mutate=mutate)
    return _real_validator(harness), replace(admission, knowledge_evidence=changed)


def _case_reader_author_departed(harness, evidence, admission):
    harness.registry.chats[(CHANNEL, GROUP)] = [OTHER]
    return _real_validator(harness), admission


def _case_proven_member_added(harness, evidence, admission):
    harness.registry.chats[(CHANNEL, GROUP)] = [AUTHOR, OTHER, "491700000000"]
    return _real_validator(harness), admission


def _case_stored_policy_revision_differs(harness, evidence, admission):
    validator = _custom_validator(
        selector=_real_selector(harness),
        chat_registry=harness.registry,
        knowledge=_RevisionKnowledge(
            harness.knowledge, int(harness.knowledge.policy_revision) + 1
        ),
    )
    return validator, admission


def _case_evidence_version_unsupported(harness, evidence, admission):
    changed = dict(evidence, version=int(evidence["version"]) + 1)
    return _real_validator(harness), replace(admission, knowledge_evidence=changed)


def _case_evidence_readers_missing(harness, evidence, admission):
    changed = dict(evidence, readers=[])
    return _real_validator(harness), replace(admission, knowledge_evidence=changed)


def _case_evidence_prerequisites_missing(harness, evidence, admission):
    changed = {key: value for key, value in evidence.items() if key != "reader_prerequisites"}
    return _real_validator(harness), replace(admission, knowledge_evidence=changed)


def _case_evidence_not_a_mapping(harness, evidence, admission):
    del evidence
    return _real_validator(harness), replace(admission, knowledge_evidence="not-a-mapping")


def _case_evidence_unreadable(harness, evidence, admission):
    unreadable = _UnreadableEvidence(evidence)
    return _real_validator(harness), replace(admission, knowledge_evidence=unreadable)


def _case_registry_missing(harness, evidence, admission):
    validator = _custom_validator(
        selector=_real_selector(harness), chat_registry=None, knowledge=harness.knowledge
    )
    return validator, admission


def _case_registry_unreadable(harness, evidence, admission):
    validator = _custom_validator(
        selector=_real_selector(harness),
        chat_registry=_BrokenRegistry(),
        knowledge=harness.knowledge,
    )
    return validator, admission


def _case_knowledge_store_unavailable(harness, evidence, admission):
    selector = ParticipationKnowledgeSelector(
        knowledge=_BrokenKnowledge(), memory=harness.memory
    )
    validator = _custom_validator(
        selector=selector, chat_registry=harness.registry, knowledge=harness.knowledge
    )
    return validator, admission


def _case_revalidation_unsupported(harness, evidence, admission):
    validator = _custom_validator(
        selector=SimpleNamespace(), chat_registry=harness.registry, knowledge=harness.knowledge
    )
    return validator, admission


def _case_policy_revision_unavailable(harness, evidence, admission):
    validator = _custom_validator(
        selector=_real_selector(harness),
        chat_registry=harness.registry,
        knowledge=_RevisionKnowledge(harness.knowledge, "1"),
    )
    return validator, admission


_VALIDATOR_TABLE: list[tuple[str, Any, tuple[bool, str]]] = [
    ("unchanged", _case_unchanged, (True, "allow")),
    ("legacy_without_evidence", _case_legacy_without_evidence, (True, "allow")),
    ("statement_revoked", _case_statement_revoked, (False, "knowledge_changed")),
    (
        "suppressed_text_with_live_records",
        _case_suppressed_text_with_live_records,
        (False, "knowledge_changed"),
    ),
    ("rendered_text_changed", _case_rendered_text_changed, (False, "knowledge_changed")),
    ("record_text_changed", _case_record_text_changed, (False, "knowledge_changed")),
    ("source_revision_changed", _case_source_revision_changed, (False, "knowledge_changed")),
    ("source_event_unknown", _case_source_event_unknown, (False, "knowledge_changed")),
    (
        "reader_author_departed",
        _case_reader_author_departed,
        (False, "knowledge_reader_authority_changed"),
    ),
    (
        "proven_member_added",
        _case_proven_member_added,
        (False, "knowledge_reader_authority_changed"),
    ),
    (
        "stored_policy_revision_differs",
        _case_stored_policy_revision_differs,
        (False, "knowledge_reader_authority_changed"),
    ),
    (
        "evidence_version_unsupported",
        _case_evidence_version_unsupported,
        (False, "knowledge_evidence_invalid"),
    ),
    (
        "evidence_readers_missing",
        _case_evidence_readers_missing,
        (False, "knowledge_reader_authority_changed"),
    ),
    (
        "evidence_prerequisites_missing",
        _case_evidence_prerequisites_missing,
        (False, "knowledge_reader_authority_changed"),
    ),
    (
        "evidence_not_a_mapping",
        _case_evidence_not_a_mapping,
        (False, "knowledge_evidence_unreadable"),
    ),
    (
        "evidence_unreadable",
        _case_evidence_unreadable,
        (False, "knowledge_reader_authority_unavailable"),
    ),
    (
        "registry_missing",
        _case_registry_missing,
        (False, "knowledge_reader_authority_changed"),
    ),
    (
        "registry_unreadable",
        _case_registry_unreadable,
        (False, "knowledge_reader_authority_changed"),
    ),
    (
        "knowledge_store_unavailable",
        _case_knowledge_store_unavailable,
        (False, "knowledge_changed"),
    ),
    (
        "revalidation_unsupported",
        _case_revalidation_unsupported,
        (False, "knowledge_revalidation_unavailable"),
    ),
    (
        "policy_revision_unavailable",
        _case_policy_revision_unavailable,
        (False, "knowledge_reader_authority_unavailable"),
    ),
]


@pytest.mark.parametrize(
    "mutate,expected", [(case, expected) for _, case, expected in _VALIDATOR_TABLE],
    ids=[name for name, _, _ in _VALIDATOR_TABLE],
)
def test_knowledge_validator_decision_table(tmp_path: Path, mutate, expected) -> None:
    """Every situation resolves to the decision the validator really returns."""
    harness = CaptureHarness(tmp_path)
    try:
        _capture_shared_statement(harness)
        evidence = _real_evidence(harness)
        baseline = _real_admission(evidence)
        # Every case starts from evidence this same validator accepts.
        assert _real_validator(harness)(baseline) == (True, "allow")

        validator, admission = mutate(harness, evidence, baseline)

        assert validator(admission) == expected
    finally:
        harness.close()


@pytest.mark.parametrize(
    "mutate",
    [
        _case_registry_missing,
        _case_registry_unreadable,
        _case_knowledge_store_unavailable,
        _case_evidence_unreadable,
        _case_revalidation_unsupported,
        _case_policy_revision_unavailable,
    ],
    ids=[
        "registry_missing",
        "registry_unreadable",
        "knowledge_store_unavailable",
        "evidence_unreadable",
        "revalidation_unsupported",
        "policy_revision_unavailable",
    ],
)
def test_knowledge_validator_fails_closed_when_it_cannot_read_authority(
    tmp_path: Path, mutate
) -> None:
    """An unreadable authority never becomes an approval."""
    harness = CaptureHarness(tmp_path)
    try:
        _capture_shared_statement(harness)
        evidence = _real_evidence(harness)
        baseline = _real_admission(evidence)

        validator, admission = mutate(harness, evidence, baseline)
        allowed, reason = validator(admission)

        assert allowed is False
        assert reason != "allow"
    finally:
        harness.close()


# -- D. approval commit ---------------------------------------------------------------


async def test_approval_commit_refuses_a_revoked_statement(tmp_path: Path) -> None:
    """The owner approval is revalidated at the commit, before any send."""
    harness = CaptureHarness(tmp_path)
    tools, log, archive, effects = _build_tools(tmp_path)
    try:
        _capture_shared_statement(harness)
        evidence = _real_evidence(harness)
        validator = _real_validator(harness)
        assert validator(_real_admission(evidence)) == (True, "allow")
        assert (await _stage(tools, knowledge_evidence=evidence, chat=GROUP))["status"] == (
            "awaiting_approval"
        )
        snapshot = await _stored_snapshot(log)
        await _claim(log, snapshot)
        assert await log.reserve_delivery(
            proposal_id="opp-1",
            effect_id=_effect_id(GROUP),
            channel=CHANNEL,
            chat_id=GROUP,
            now_ms=NOW,
            limits=(("comment", 1, 3_600_000),),
            origin="participation",
            lane="production",
        )

        submission = _Submission()
        tools.set_participation_submission(submission)
        tools.set_knowledge_decision_validator(validator)
        preview_calls = list(effects.calls)

        # Knowledge changed after the owner approved and before the commit.
        assert harness.delete(REAL_KEY) == (REAL_KEY,)

        outcome = await tools.submit_proposal("opp-1")

        assert outcome == {
            "status": "rejected",
            "reason": "knowledge_approval_knowledge_changed",
        }
        assert submission.calls == []
        assert effects.calls == preview_calls
        record = await log.delivery_record(proposal_id="opp-1", effect_id=_effect_id(GROUP))
        assert record is not None
        assert record["delivery_state"] == "failed"
        assert record["attempt_state"] == "released"
        assert record["evidence_ref"] == "knowledge_approval_knowledge_changed"
        row = await log.proposal_row("opp-1")
        assert row is not None
        assert row["status"] == "rejected"
        stored = (await _stored_snapshot(log))["participation_admission"]
        assert stored["knowledge_evidence"] == evidence
    finally:
        log.close()
        archive.close()
        harness.close()


async def test_a_refused_stale_approval_is_not_regenerated(tmp_path: Path) -> None:
    """The refusal ends the approval: no new draft, no replacement payload, no second effect."""
    harness = CaptureHarness(tmp_path)
    tools, log, archive, effects = _build_tools(tmp_path)
    try:
        _capture_shared_statement(harness)
        evidence = _real_evidence(harness)
        assert (await _stage(tools, knowledge_evidence=evidence, chat=GROUP))["status"] == (
            "awaiting_approval"
        )
        snapshot = await _stored_snapshot(log)
        await _claim(log, snapshot)
        assert await log.reserve_delivery(
            proposal_id="opp-1",
            effect_id=_effect_id(GROUP),
            channel=CHANNEL,
            chat_id=GROUP,
            now_ms=NOW,
            limits=(("comment", 1, 3_600_000),),
            origin="participation",
            lane="production",
        )

        submission = _Submission()
        tools.set_participation_submission(submission)
        tools.set_knowledge_decision_validator(
            lambda admission: (False, "knowledge_reader_authority_changed")
        )
        preview_calls = list(effects.calls)

        outcome = await tools.submit_proposal("opp-1")

        assert outcome == {
            "status": "rejected",
            "reason": "knowledge_approval_knowledge_reader_authority_changed",
        }
        # The original content never left, and nothing replaced it.
        assert submission.calls == []
        assert effects.calls == preview_calls
        proposals = log._conn.execute(
            "SELECT id, message, status FROM speakups WHERE id = 'opp-1'"
        ).fetchall()
        assert [dict(row) for row in proposals] == [
            {"id": "opp-1", "message": "the prepared draft", "status": "rejected"}
        ]
        reservations = log._conn.execute(
            "SELECT effect_id, delivery_state FROM delivery_reservations"
            " WHERE proposal_id = 'opp-1'"
        ).fetchall()
        # Exactly the original managed effect, released - no second one was created.
        assert [dict(row) for row in reservations] == [
            {"effect_id": _effect_id(GROUP), "delivery_state": "failed"}
        ]
        stored = (await _stored_snapshot(log))["participation_admission"]
        assert stored["knowledge_evidence"] == evidence
        assert stored["payload_hash"] == payload_hash(TextPayload(text="the prepared draft"))
    finally:
        log.close()
        archive.close()
        harness.close()


async def test_persisted_evidence_revalidates_after_a_full_restart(tmp_path: Path) -> None:
    """Fresh validator and fresh tools over the same SQLite files still allow the commit."""
    harness = CaptureHarness(tmp_path)
    tools, log, archive, _effects = _build_tools(tmp_path)
    try:
        _capture_shared_statement(harness)
        evidence = _real_evidence(harness)
        assert (await _stage(tools, knowledge_evidence=evidence, chat=GROUP))["status"] == (
            "awaiting_approval"
        )
        snapshot = await _stored_snapshot(log)
        await _claim(log, snapshot)
    finally:
        log.close()
        archive.close()
        harness.close()

    restarted = CaptureHarness(tmp_path)
    restarted_tools, restarted_log, restarted_archive, _ = _build_tools(tmp_path)
    try:
        validator = _real_validator(restarted)
        stored = (await _stored_snapshot(restarted_log))["participation_admission"]
        values = dict(stored)
        values["source_event_ids"] = tuple(values["source_event_ids"])
        values["source_principals"] = tuple(
            (str(pair[0]), str(pair[1])) for pair in values["source_principals"]
        )
        reconstruction = ParticipationAdmission(**values)
        assert reconstruction.knowledge_evidence == evidence
        assert validator(reconstruction) == (True, "allow")

        restarted_tools.set_knowledge_decision_validator(validator)
        submission = _Submission()
        restarted_tools.set_participation_submission(submission)

        outcome = await restarted_tools.submit_proposal("opp-1")

        assert outcome["status"] == "submitted"
        assert len(submission.calls) == 1
        admission = submission.calls[0]["admission"]
        assert isinstance(admission, ParticipationAdmission)
        assert admission.knowledge_evidence == evidence
    finally:
        restarted_log.close()
        restarted_archive.close()
        restarted.close()


# -- D. pre-dispatch (transport) boundary ---------------------------------------------


class _ReservationLedger:
    """One submitted delivery reservation, without opening a second ledger."""

    def __init__(self, *, proposal_id: str, effect_id: str, now_ms: int) -> None:
        self._row = {
            "proposal_id": proposal_id,
            "effect_id": effect_id,
            "created_at_ms": int(now_ms),
            "attempt_state": "submitted",
        }

    def _delivery_row_for_effect(self, effect_id: str):
        if str(effect_id) != self._row["effect_id"]:
            return None
        return dict(self._row)

    def material_for_opportunity(self, channel, chat_id, sources, *, lane=None):
        del channel, chat_id, sources, lane
        return (False, 0)


class _SourceArchive:
    def __init__(self, senders: dict[str, str]) -> None:
        self._senders = dict(senders)

    def senders_for_messages(self, channel: str, chat_id: str, source_ids):
        del channel, chat_id
        wanted = {str(item) for item in source_ids}
        return {key: value for key, value in self._senders.items() if key in wanted}


class _StaticAdapter:
    """A policy adapter that lets the transport authorization pass."""

    def __init__(self, engine: object) -> None:
        self._engine = engine
        self._snapshot = PolicySnapshot(
            version=DISPATCH_POLICY_VERSION,
            policy_hash=DISPATCH_POLICY_HASH,
            policy=None,
            loaded_ms=0,
            healthy=True,
            source="in-memory",
            error=None,
        )
        self.known_tools = {"message"}

    def current_activation(self, channel: str, chat_id: str):
        del channel, chat_id
        return SimpleNamespace(
            enabled=True,
            valid=True,
            opted_in=True,
            shadow=False,
            activation_epoch=1,
            lane="production",
            policy_version=DISPATCH_POLICY_VERSION,
            participation=SimpleNamespace(
                allow_initiation=True, allow_continuation=True, allow_reactions=True
            ),
        )

    def policy_engine(self):
        return self._engine

    def policy_snapshot(self):
        return self._snapshot

    def participation_pause_reason(self, channel: str, chat_id: str):
        del channel, chat_id
        return None


def _dispatch_config() -> object:
    from yeoman_shared.config.schema import Config, ConsciousnessConfig, ProcessingConfig

    return Config(
        consciousness=ConsciousnessConfig.model_validate({"defaultDailyCap": 3}),
        processing=ProcessingConfig.model_validate(
            {
                "enabled": True,
                "chats": [f"{CHANNEL}:{GROUP}"],
                "reply_actions": {f"{CHANNEL}:{GROUP}": "answer"},
                "participation": {
                    "enabled": True,
                    "shadow": False,
                    "judgeRoute": "participation.judge",
                },
            }
        ),
    )


def _dispatch_engine(tmp_path: Path) -> PolicyEngine:
    return PolicyEngine(
        PolicyConfig.model_validate(
            {
                "defaults": {"allowedTools": {"mode": "allowlist", "tools": ["message"]}},
                "channels": {
                    CHANNEL: {
                        "chats": {
                            GROUP: {
                                "whoCanTalk": {"mode": "everyone"},
                                "whenToReply": {"mode": "all"},
                                "spontaneity": {
                                    "enabled": True,
                                    "dailyCap": 3,
                                    "allowedActions": ["observation"],
                                    "preview": "owner_dm",
                                },
                                "participation": {"enabled": True},
                            }
                        }
                    }
                },
            }
        ),
        workspace=tmp_path,
    )


def _dispatch_router(
    tmp_path: Path,
    *,
    store: ProcessingStore,
    validator: object,
    evidence: dict[str, Any] | None = None,
    effect_id: str = "effect-dispatch-1",
):
    """The production effect router whose pre-dispatch hook carries the validator."""
    from yeoman_gateway.app.bootstrap import build_effect_router
    from yeoman_gateway.bus.queue import MessageBus

    envelope = _envelope(effect_id, channel=CHANNEL, chat_id=GROUP)
    ledger = _ReservationLedger(
        proposal_id="opp-1", effect_id=effect_id, now_ms=int(time.time() * 1000)
    )
    router = build_effect_router(
        _dispatch_config(),
        _StaticAdapter(_dispatch_engine(tmp_path)),
        store,
        MessageBus(),
        participation_ledger=ledger,
        inbound_archive=_SourceArchive({"m1": "alice"}),
        knowledge_decision_validator=validator,
    )
    assert router is not None
    transport: list[object] = []

    async def record_text(message: object):
        transport.append(message)
        return {"provider_message_id": "provider-1"}

    async def reject_reaction(message: object):
        raise AssertionError("a comment must not reach the reaction transport")

    router.set_direct_transport(record_text, reject_reaction)
    store.enqueue_participation_effect(
        envelope,
        _admission(
            payload_hash=envelope.payload_hash,
            channel=CHANNEL,
            chat_id=GROUP,
            arbitration_revision=0,
            knowledge_evidence=evidence,
        ),
    )
    return router, envelope, transport


def _effect_evidence(store: ProcessingStore, effect_id: str) -> list[str]:
    meta = store.effect_meta(effect_id)
    assert meta is not None
    return [str(entry.detail) for entry in meta.evidence]


def test_pre_dispatch_blocks_transport_when_the_validator_refuses(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "processing.db")
    try:
        asked: list[str] = []

        def refuse(admission: object) -> tuple[bool, str]:
            asked.append(str(getattr(admission, "admission_id", "")))
            return False, "knowledge_changed"

        router, envelope, transport = _dispatch_router(
            tmp_path, store=store, validator=refuse
        )
        assert store.count_effects() == 1

        receipt = asyncio.run(router._gateway.execute_ready(envelope.effect_id))

        assert receipt.state == "failed"
        assert asked == ["adm-1"]
        assert transport == []
        assert store.count_effects() == 1
        assert store.effect_state(envelope.effect_id) == "failed"
        assert any(
            "ParticipationPreDispatchDenied: knowledge_approval_knowledge_changed" in detail
            for detail in _effect_evidence(store, envelope.effect_id)
        )
    finally:
        store.close()


def test_invalidated_evidence_between_approval_and_dispatch_blocks_transport(
    tmp_path: Path,
) -> None:
    """Evidence true at approval time, revoked before transport: the send is blocked."""
    harness = CaptureHarness(tmp_path)
    try:
        _capture_shared_statement(harness)
        evidence = _real_evidence(harness)
        validator = _real_validator(harness)
        admission = _real_admission(evidence)
        assert validator(admission) == (True, "allow")

        router, envelope, transport = _dispatch_router(
            tmp_path, store=harness.store, validator=validator, evidence=evidence
        )

        # The statement is revoked after the owner approved and before dispatch.
        assert harness.delete(REAL_KEY) == (REAL_KEY,)
        assert any(row["status"] == "revoked" for row in harness.statements())
        assert validator(admission) == (False, "knowledge_changed")

        receipt = asyncio.run(router._gateway.execute_ready(envelope.effect_id))

        assert receipt.state == "failed"
        assert transport == []
        assert harness.store.effect_state(envelope.effect_id) == "failed"
        assert any(
            "ParticipationPreDispatchDenied: knowledge_approval_knowledge_changed" in detail
            for detail in _effect_evidence(harness.store, envelope.effect_id)
        )
    finally:
        harness.close()


# -- D. the runtime's preserved fallback and legacy adapter ---------------------------


class _LegacyApprovalSubmission:
    """A submission adapter that predates durable evidence.

    Its signature deliberately has no ``knowledge_evidence`` parameter: if the runtime
    passed one when there is nothing to bind, this adapter would raise instead of queueing.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def queue_approval(
        self, *, opportunity, decision, admission, effect_id, content, snapshot
    ):
        self.calls.append(
            {
                "opportunity": opportunity,
                "decision": decision,
                "admission": admission,
                "effect_id": effect_id,
                "content": content,
                "snapshot": snapshot,
            }
        )
        return {"status": "awaiting_approval", "effect_id": effect_id}


def _participation_runtime(tmp_path: Path, submission: object) -> ParticipationRuntime:
    """The real runtime, wired only as far as the approval-queue branch needs."""
    return ParticipationRuntime(
        judge=None,
        context_builder=object(),
        ledger=SpeakupLog(tmp_path / "runtime-speakups.db"),
        snapshot_provider=lambda channel, chat_id, **kwargs: {},
        is_source_allowed=lambda channel, chat_id, sources: True,
        source_principals=lambda channel, chat_id, sources: tuple(
            (str(source), "anna@s.whatsapp.net") for source in sources
        ),
        submission=submission,
        clock_ms=lambda: NOW,
    )


async def _submit_approval(runtime: ParticipationRuntime, *, context) -> dict[str, object]:
    return await runtime._submit_comment(
        opportunity=_opportunity(),
        snapshot={
            "approval_required": True,
            "lane": "production",
            "activation_epoch": 1,
            "opportunity_ttl_seconds": 3600,
        },
        inputs=SimpleNamespace(),
        decision=_decision(),
        text="the prepared draft",
        context=context,
        effect_id=_effect_id(),
        evaluation_index=0,
    )


async def test_knowledge_backed_draft_without_evidence_keeps_the_fallback(
    tmp_path: Path,
) -> None:
    """A knowledge-backed draft with no durable evidence is still refused as before."""
    submission = _LegacyApprovalSubmission()
    runtime = _participation_runtime(tmp_path, submission)
    try:
        # The selection is unchanged, so only the missing evidence can refuse the queue.
        runtime._revalidate_knowledge = lambda opportunity, context, current: current
        selection = SimpleNamespace(text="selected knowledge", revision="r1")

        result = await _submit_approval(runtime, context={"_knowledge_selection": selection})

        assert result == {
            "status": "comment_skipped",
            "reason": "approval_knowledge_revalidation_unavailable",
        }
        assert submission.calls == []
    finally:
        runtime._ledger.close()


async def test_legacy_recent_only_proposal_still_queues_without_the_kwarg(
    tmp_path: Path,
) -> None:
    """No evidence means no ``knowledge_evidence`` kwarg, so an old adapter keeps working."""
    submission = _LegacyApprovalSubmission()
    runtime = _participation_runtime(tmp_path, submission)
    try:
        result = await _submit_approval(runtime, context={})

        assert result["status"] == "awaiting_approval"
        assert len(submission.calls) == 1
        assert submission.calls[0]["content"] == "the prepared draft"
        assert submission.calls[0]["admission"].knowledge_evidence is None
    finally:
        runtime._ledger.close()
