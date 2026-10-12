from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch

from test_memory_cli import isolated_memory_cli_config  # noqa: F401 - shared autouse fixture
from typer.testing import CliRunner
from yeoman_gateway.knowledge._memory.disclosure import (
    classify_disclosure_for_content,
    render_disclosed_hits,
)
from yeoman_gateway.knowledge._memory.disclosure_backfill import (
    DisclosureTagSuggestion,
    NarrowDisclosureClassifier,
    parse_suggestions,
    run_disclosure_backfill,
)
from yeoman_gateway.knowledge._memory.extractor import _SYSTEM_PROMPT
from yeoman_gateway.knowledge._memory.models import MemoryEntry, MemoryHit
from yeoman_gateway.knowledge._memory.service import MemoryService
from yeoman_gateway.providers.base import LLMProvider, LLMResponse
from yeoman_shared.config.schema import Config

runner = CliRunner()


class CaptureProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__()
        self.messages_seen: list[list[dict[str, Any]]] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning: dict[str, Any] | None = None,
    ) -> LLMResponse:
        del tools, model, max_tokens, temperature, reasoning
        self.messages_seen.append(messages)
        return LLMResponse(content="ok")

    def get_default_model(self) -> str:
        return "dummy/model"


class FakeDisclosureClassifier:
    async def classify(self, entries: list[MemoryEntry]) -> list[DisclosureTagSuggestion]:
        return [
            DisclosureTagSuggestion(
                entry_id=entry.id,
                topics=("family",),
                sensitivity="private",
                disclosure_mode="owner_only",
                subjects=("timo",),
            )
            for entry in entries
        ]


def _entry(content: str, metadata: dict[str, object] | str | None = None) -> MemoryEntry:
    meta_json = metadata if isinstance(metadata, str) else json.dumps(metadata or {})
    return MemoryEntry(
        id=str(uuid.uuid4()),
        workspace_id="workspace",
        scope_type="chat",
        scope_key="channel:whatsapp:chat:test@g.us",
        channel="whatsapp",
        chat_id="test@g.us",
        sender_id="sender",
        sector="semantic",
        kind="fact",
        content=content,
        content_norm=content.lower(),
        content_hash="hash",
        salience=0.8,
        confidence=0.9,
        source="test",
        meta_json=meta_json,
    )


def _hit(content: str, metadata: dict[str, object] | str | None = None) -> MemoryHit:
    hit = MemoryHit(entry=_entry(content, metadata))
    hit.final_score = 0.8
    return hit


def test_extractor_prompt_requires_splitting_mixed_disclosure_boundaries() -> None:
    assert "different sensitivity" in _SYSTEM_PROMPT
    assert "different disclosure behavior" in _SYSTEM_PROMPT
    assert "output separate memory candidates" in _SYSTEM_PROMPT


def test_narrow_policy_treats_outside_world_conflict_as_speakable() -> None:
    metadata = classify_disclosure_for_content(
        "Trump comments on Iran war escalation and the impact on oil prices.",
        base={"topics": ["politics", "war"], "sensitivity": "taboo"},
    )

    assert metadata.sensitivity == "normal"
    assert metadata.disclosure_mode == "speakable"
    assert "war" in metadata.topics


def test_narrow_policy_treats_public_figure_family_death_as_speakable() -> None:
    metadata = classify_disclosure_for_content(
        "Dario Amodei: father died of a rare illness that became curable later.",
        base={"topics": ["family", "health"], "sensitivity": "taboo"},
    )

    assert metadata.sensitivity == "normal"
    assert metadata.disclosure_mode == "speakable"


def test_narrow_policy_treats_public_meme_health_topic_as_speakable() -> None:
    metadata = classify_disclosure_for_content(
        "[group_notes_batch] [4915774497527] [Image] [image_description] "
        "This image is a meme featuring Donald Trump in a hospital gown.",
        base={"topics": ["humor", "politics", "health"], "sensitivity": "taboo"},
    )

    assert metadata.sensitivity == "normal"
    assert metadata.disclosure_mode == "speakable"


def test_narrow_policy_treats_public_celebrity_death_news_as_speakable() -> None:
    metadata = classify_disclosure_for_content(
        "[group_notes_batch] [4915256139011] [Image] [image_description] "
        "This is a screenshot of a German news article reporting that action star Chuck Norris died.",
        base={"topics": ["humor"], "sensitivity": "taboo"},
    )

    assert metadata.sensitivity == "normal"
    assert metadata.disclosure_mode == "speakable"


def test_narrow_policy_treats_public_first_person_meme_text_as_speakable() -> None:
    metadata = classify_disclosure_for_content(
        "[group_notes_batch] [4917632625469] [Image] [image_description] "
        'A presentation meme about curing cancer includes the text "I went founder mode."',
        base={"topics": ["humor", "health"], "sensitivity": "taboo"},
    )

    assert metadata.sensitivity == "normal"
    assert metadata.disclosure_mode == "speakable"


def test_narrow_policy_restricts_group_relative_funeral_context() -> None:
    metadata = classify_disclosure_for_content(
        "Gerade viel unterwegs, besucht Beerdigung mit Mutter, empfindet es als anstrengend.",
        base={"topics": ["family", "health"], "sensitivity": "normal"},
        scope_type="contact",
    )

    assert metadata.sensitivity == "taboo"
    assert metadata.disclosure_mode == "never_initiate"
    assert "funeral" in metadata.topics


def test_narrow_policy_restricts_first_person_chronic_illness() -> None:
    metadata = classify_disclosure_for_content(
        "Ich bin seit Anfang Januar durchgehend krank, mal eine Woche Pause, dann wieder.",
        base={"topics": ["health"], "sensitivity": "normal"},
    )

    assert metadata.sensitivity == "taboo"
    assert metadata.disclosure_mode == "never_initiate"
    assert "health" in metadata.topics


def test_narrow_policy_restricts_group_self_harm_reference() -> None:
    metadata = classify_disclosure_for_content(
        "[group_notes_batch] [4915253696948] Vielleicht muss ich mich doch nicht ritzen.",
        base={"topics": ["health", "emotional"], "sensitivity": "normal"},
    )

    assert metadata.sensitivity == "taboo"
    assert metadata.disclosure_mode == "never_initiate"


def _make_service(tmp_path: Path) -> MemoryService:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    cfg = Config()
    cfg.memory.db_path = str(tmp_path / "memory.db")
    cfg.memory.capture.enabled = False
    cfg.memory.embedding.enabled = False
    with patch("yeoman_gateway.knowledge._memory.service._load_owner_ids", return_value={}):
        return MemoryService(workspace=workspace, config=cfg.memory)


def _insert_legacy_entry(service: MemoryService, content: str) -> MemoryEntry:
    entry = MemoryEntry(
        id=str(uuid.uuid4()),
        workspace_id=service.workspace_id,
        scope_type="chat",
        scope_key=service.chat_scope_key("whatsapp", "group@g.us"),
        channel="whatsapp",
        chat_id="group@g.us",
        sender_id="owner@s.whatsapp.net",
        sector="semantic",
        kind="fact",
        content=content,
        content_norm=content.lower(),
        content_hash=service._hash_content(content),
        salience=0.9,
        confidence=0.9,
        source="test",
        meta_json="{}",
    )
    saved, _ = service.store.upsert_node(entry)
    return saved


def _insert_tagged_entry(
    service: MemoryService,
    content: str,
    metadata: dict[str, object],
) -> MemoryEntry:
    entry = MemoryEntry(
        id=str(uuid.uuid4()),
        workspace_id=service.workspace_id,
        scope_type="chat",
        scope_key=service.chat_scope_key("whatsapp", "group@g.us"),
        channel="whatsapp",
        chat_id="group@g.us",
        sender_id="owner@s.whatsapp.net",
        sector="semantic",
        kind="fact",
        content=content,
        content_norm=content.lower(),
        content_hash=service._hash_content(content),
        salience=0.9,
        confidence=0.9,
        source="test",
        meta_json=json.dumps(metadata),
    )
    saved, _ = service.store.upsert_node(entry)
    return saved


def test_taboo_memory_renders_guardrail_without_raw_content() -> None:
    hit = _hit(
        "Timo is quiet because his father died last year.",
        {
            "topics": ["funeral"],
            "sensitivity": "taboo",
            "disclosure_mode": "never_initiate",
        },
    )

    rendered = render_disclosed_hits(
        [hit],
        query="Why is Timo quiet?",
        owner_context=False,
        max_chars=1200,
    )

    assert "father died" not in rendered
    assert "funeral" not in rendered
    assert "[Private Context Guardrails]" in rendered
    assert "Do not reveal" in rendered


def test_taboo_memory_renders_raw_when_owner_explicitly_raises_topic() -> None:
    hit = _hit(
        "Timo is quiet because his father died last year.",
        {
            "topics": ["funeral"],
            "sensitivity": "taboo",
            "disclosure_mode": "never_initiate",
        },
    )

    rendered = render_disclosed_hits(
        [hit],
        query="Can you remind me about the funeral context?",
        owner_context=True,
        max_chars=1200,
    )

    assert "[Retrieved Memory]" in rendered
    assert "father died" in rendered


def test_sensitive_memory_renders_raw_when_topic_is_explicit() -> None:
    hit = _hit(
        "Alex is worried about a health appointment.",
        {
            "topics": ["health"],
            "sensitivity": "sensitive",
            "disclosure_mode": "context_only",
        },
    )

    rendered = render_disclosed_hits(
        [hit],
        query="Any health update from Alex?",
        owner_context=False,
        max_chars=1200,
    )

    assert "health appointment" in rendered


def test_malformed_metadata_behaves_like_normal_memory() -> None:
    hit = _hit("I prefer concise answers.", "{not json")

    rendered = render_disclosed_hits(
        [hit],
        query="concise",
        owner_context=False,
        max_chars=1200,
    )

    assert "I prefer concise answers" in rendered


def test_record_manual_persists_disclosure_metadata(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    try:
        entry, inserted = service.record_manual(
            channel="whatsapp",
            chat_id="group@g.us",
            sender_id="owner@s.whatsapp.net",
            scope_type="chat",
            kind="fact",
            text="Private family funeral context.",
            importance=0.9,
            topics=["funeral", "family"],
            sensitivity="taboo",
            disclosure_mode="never_initiate",
            subjects=["timo"],
        )
    finally:
        service.close()

    assert inserted is True
    metadata = json.loads(entry.meta_json)
    assert metadata["topics"] == ["funeral", "family"]
    assert metadata["sensitivity"] == "taboo"
    assert metadata["disclosure_mode"] == "never_initiate"
    assert metadata["subjects"] == ["timo"]


def test_update_disclosure_metadata_changes_existing_entry(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    try:
        entry, _ = service.record_manual(
            channel="whatsapp",
            chat_id="group@g.us",
            sender_id="owner@s.whatsapp.net",
            scope_type="chat",
            kind="fact",
            text="Private family funeral context.",
            importance=0.9,
        )

        updated = service.update_disclosure_metadata(
            entry.id,
            topics=["funeral"],
            sensitivity="private",
            disclosure_mode="owner_only",
            subjects=["timo"],
        )
    finally:
        service.close()

    assert updated is not None
    metadata = json.loads(updated.meta_json)
    assert metadata["topics"] == ["funeral"]
    assert metadata["sensitivity"] == "private"
    assert metadata["disclosure_mode"] == "owner_only"
    assert metadata["subjects"] == ["timo"]


def test_parse_suggestions_normalizes_model_payload() -> None:
    suggestions = parse_suggestions(
        """
        ```json
        {"items":[{"id":"abc","topics":["Family Life",""],"sensitivity":"PRIVATE","subjects":["Timo"]}]}
        ```
        """
    )

    assert suggestions == [
        DisclosureTagSuggestion(
            entry_id="abc",
            topics=("family_life",),
            sensitivity="private",
            disclosure_mode="owner_only",
            subjects=("timo",),
        )
    ]


def test_parse_suggestions_derives_disclosure_from_sensitivity() -> None:
    suggestions = parse_suggestions(
        '{"items":[{"id":"abc","topics":["voice"],"sensitivity":"normal","disclosure_mode":"context_only"}]}'
    )

    assert suggestions[0].sensitivity == "normal"
    assert suggestions[0].disclosure_mode == "speakable"


def test_disclosure_backfill_dry_run_does_not_update_rows(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    try:
        entry = _insert_legacy_entry(service, "Private family context.")

        result = asyncio.run(
            run_disclosure_backfill(
                memory=service,
                classifier=FakeDisclosureClassifier(),
                apply=False,
            )
        )
        unchanged = service.store.get_node(entry.id, workspace_id=service.workspace_id)
    finally:
        service.close()

    assert result.scanned == 1
    assert result.suggested == 1
    assert result.applied == 0
    assert unchanged is not None
    assert json.loads(unchanged.meta_json) == {}


def test_disclosure_backfill_apply_updates_missing_rows(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    try:
        entry = _insert_legacy_entry(service, "Private family context.")

        result = asyncio.run(
            run_disclosure_backfill(
                memory=service,
                classifier=FakeDisclosureClassifier(),
                apply=True,
                backup=False,
            )
        )
        updated = service.store.get_node(entry.id, workspace_id=service.workspace_id)
    finally:
        service.close()

    assert result.scanned == 1
    assert result.applied == 1
    assert updated is not None
    metadata = json.loads(updated.meta_json)
    assert metadata["topics"] == ["family"]
    assert metadata["sensitivity"] == "private"
    assert metadata["disclosure_mode"] == "owner_only"


def test_disclosure_backfill_all_workspaces_updates_legacy_rows(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    try:
        current = _insert_legacy_entry(service, "Private family context.")
        other = MemoryEntry(
            id=str(uuid.uuid4()),
            workspace_id="other-workspace",
            scope_type="chat",
            scope_key=service.chat_scope_key("whatsapp", "group@g.us"),
            channel="whatsapp",
            chat_id="group@g.us",
            sender_id="owner@s.whatsapp.net",
            sector="semantic",
            kind="fact",
            content="Other workspace private family context.",
            content_norm="other workspace private family context.",
            content_hash=service._hash_content("Other workspace private family context."),
            salience=0.9,
            confidence=0.9,
            source="test",
            meta_json="{}",
        )
        other_saved, _ = service.store.upsert_node(other)

        result = asyncio.run(
            run_disclosure_backfill(
                memory=service,
                classifier=FakeDisclosureClassifier(),
                apply=True,
                backup=False,
                all_workspaces=True,
            )
        )
        current_updated = service.store.get_node(current.id, workspace_id=service.workspace_id)
        other_updated = service.store.get_node(other_saved.id, workspace_id="other-workspace")
    finally:
        service.close()

    assert result.scanned == 2
    assert result.applied == 2
    assert current_updated is not None
    assert other_updated is not None
    assert json.loads(current_updated.meta_json)["sensitivity"] == "private"
    assert json.loads(other_updated.meta_json)["sensitivity"] == "private"


def test_narrow_backfill_retags_broad_outside_world_labels(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    try:
        war = _insert_tagged_entry(
            service,
            "Iran war escalation could move oil prices and financial markets.",
            {"topics": ["war", "finance"], "sensitivity": "taboo", "disclosure_mode": "never_initiate"},
        )
        illness = _insert_tagged_entry(
            service,
            "Ich bin seit Anfang Januar durchgehend krank.",
            {"topics": ["health"], "sensitivity": "normal", "disclosure_mode": "speakable"},
        )

        result = asyncio.run(
            run_disclosure_backfill(
                memory=service,
                classifier=NarrowDisclosureClassifier(),
                only_missing=False,
                apply=True,
                backup=False,
            )
        )
        war_updated = service.store.get_node(war.id, workspace_id=service.workspace_id)
        illness_updated = service.store.get_node(illness.id, workspace_id=service.workspace_id)
    finally:
        service.close()

    assert result.scanned == 2
    assert result.applied == 2
    assert war_updated is not None
    assert illness_updated is not None
    assert json.loads(war_updated.meta_json)["sensitivity"] == "normal"
    assert json.loads(war_updated.meta_json)["disclosure_mode"] == "speakable"
    assert json.loads(illness_updated.meta_json)["sensitivity"] == "taboo"
    assert json.loads(illness_updated.meta_json)["disclosure_mode"] == "never_initiate"


def test_auto_capture_persists_narrow_disclosure_metadata(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    try:
        service._capture_text(
            channel="whatsapp",
            chat_id="group@g.us",
            sender_id="owner@s.whatsapp.net",
            text="Iran war escalation could move oil prices.",
            role="user",
            source_message_id="war",
            mode_override="heuristic",
        )
        service._capture_text(
            channel="whatsapp",
            chat_id="group@g.us",
            sender_id="owner@s.whatsapp.net",
            text="Ich bin seit Anfang Januar durchgehend krank.",
            role="user",
            source_message_id="illness",
            mode_override="heuristic",
        )
        war = service.search(
            query="Iran war oil prices",
            channel="whatsapp",
            chat_id="group@g.us",
            scope="chat",
            limit=1,
        )[0]
        illness = service.search(
            query="Anfang Januar durchgehend krank",
            channel="whatsapp",
            chat_id="group@g.us",
            scope="chat",
            limit=1,
        )[0]
    finally:
        service.close()

    assert json.loads(war.entry.meta_json)["sensitivity"] == "normal"
    assert json.loads(war.entry.meta_json)["disclosure_mode"] == "speakable"
    assert json.loads(illness.entry.meta_json)["sensitivity"] == "taboo"
    assert json.loads(illness.entry.meta_json)["disclosure_mode"] == "never_initiate"


def test_multi_query_recall_reserves_slot_for_narrow_query_hit(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    cfg = service.config
    cfg.recall.max_results = 2
    cfg.recall.lexical_limit = 4
    try:
        broad = _entry("Alex has strong OpenAI opinions.")
        broad.workspace_id = service.workspace_id
        broad.scope_key = service.chat_scope_key("whatsapp", "group@g.us")
        broad.id = "broad"
        narrow = _entry("Natasha is the user's wife.")
        narrow.workspace_id = service.workspace_id
        narrow.scope_key = service.chat_scope_key("whatsapp", "group@g.us")
        narrow.id = "narrow"
        filler = _entry("General relationship chatter.")
        filler.workspace_id = service.workspace_id
        filler.scope_key = service.chat_scope_key("whatsapp", "group@g.us")
        filler.id = "filler"

        def fake_search_lexical(**kwargs):
            query = kwargs["query"].lower()
            if query == "natasha":
                return [MemoryHit(entry=narrow, lexical_score=0.99)]
            return [
                MemoryHit(entry=broad, lexical_score=0.95),
                MemoryHit(entry=filler, lexical_score=0.90),
            ]

        with patch.object(service.store, "search_lexical", side_effect=fake_search_lexical):
            hits = service.recall_for_event(
                channel="whatsapp",
                chat_id="group@g.us",
                sender_id="owner@s.whatsapp.net",
                query="What about Natasha and the relationship thing?",
            )
    finally:
        service.close()

    assert [hit.entry.id for hit in hits] == ["narrow", "broad"]
    assert hits[0].trace["query_origin"] == "narrow:natasha"
    assert hits[0].trace["quota"] == "reserved"






