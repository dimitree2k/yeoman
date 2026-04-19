"""Tests for person-profile contact-scope memory storage."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from yeoman_shared.config.schema import MemoryAclConfig, MemoryConfig
from yeoman_gateway.memory.extractor import ExtractedCandidate
from yeoman_gateway.memory.service import MemoryService


def _service(tmp_path: Path) -> MemoryService:
    cfg = MemoryConfig(
        db_path=str(tmp_path / "mem.db"),
        acl=MemoryAclConfig(owner_only_preference=False),
    )
    return MemoryService(workspace=tmp_path / "ws", config=cfg)


def _service_with_contact(tmp_path: Path, jid: str, contact_id: str) -> MemoryService:
    """Return a MemoryService whose contacts mock resolves jid → contact_id."""
    svc = _service(tmp_path)
    mock_contacts = MagicMock()
    mock_contacts.known_jids = {jid: contact_id}
    svc.set_contacts(mock_contacts)
    return svc


class TestContactScopeKey:
    def test_contact_scope_key_format(self) -> None:
        assert MemoryService.contact_scope_key("abc-123") == "contact:abc-123"


class TestPersistCandidatePersonProfile:
    def test_person_profile_stored_under_contact_scope(self, tmp_path: Path) -> None:
        jid = "491786@s.whatsapp.net"
        contact_id = "frank-uuid-001"
        svc = _service_with_contact(tmp_path, jid, contact_id)

        candidate = ExtractedCandidate(
            sector="semantic",
            kind="person_profile",
            content="Frank: is a doctor",
            salience=0.9,
            confidence=0.9,
        )
        svc._persist_candidate(
            channel="whatsapp",
            chat_id="group-a",
            sender_id=jid,
            role="user",
            source_message_id=None,
            candidate=candidate,
        )

        hits = svc.store.search_lexical(
            workspace_id=svc.workspace_id,
            query="Frank doctor",
            scope_keys=[MemoryService.contact_scope_key(contact_id)],
            limit=5,
        )
        assert any("Frank" in h.entry.content for h in hits)

    def test_person_profile_falls_back_to_user_scope_when_no_contact(self, tmp_path: Path) -> None:
        svc = _service(tmp_path)  # no contacts wired

        candidate = ExtractedCandidate(
            sector="semantic",
            kind="person_profile",
            content="Frank: likes hiking",
            salience=0.8,
            confidence=0.8,
        )
        svc._persist_candidate(
            channel="whatsapp",
            chat_id="group-a",
            sender_id="unknown@wa",
            role="user",
            source_message_id=None,
            candidate=candidate,
        )

        user_scope = MemoryService.user_scope_key("whatsapp", "unknown@wa")
        hits = svc.store.search_lexical(
            workspace_id=svc.workspace_id,
            query="Frank hiking",
            scope_keys=[user_scope],
            limit=5,
        )
        assert any("Frank" in h.entry.content for h in hits)

    def test_non_person_profile_unaffected(self, tmp_path: Path) -> None:
        jid = "491786@s.whatsapp.net"
        contact_id = "frank-uuid-001"
        svc = _service_with_contact(tmp_path, jid, contact_id)

        candidate = ExtractedCandidate(
            sector="semantic",
            kind="preference",
            content="prefers dark mode",
            salience=0.7,
            confidence=0.8,
        )
        svc._persist_candidate(
            channel="whatsapp",
            chat_id="group-a",
            sender_id=jid,
            role="user",
            source_message_id=None,
            candidate=candidate,
        )

        # Must be stored in user scope, NOT contact scope
        contact_hits = svc.store.search_lexical(
            workspace_id=svc.workspace_id,
            query="dark mode",
            scope_keys=[MemoryService.contact_scope_key(contact_id)],
            limit=5,
        )
        assert len(contact_hits) == 0

        user_hits = svc.store.search_lexical(
            workspace_id=svc.workspace_id,
            query="dark mode",
            scope_keys=[MemoryService.user_scope_key("whatsapp", jid)],
            limit=5,
        )
        assert len(user_hits) == 1


class TestResolveContactIdFallback:
    """_resolve_contact_id must match both the full JID and the stripped user
    token, because the live archive path and the LLM extractor emit sender IDs
    without the @s.whatsapp.net / @lid suffix, while contacts.known_jids is
    keyed by the full identifier.
    """

    def _service_with_jid(self, tmp_path: Path, full_jid: str, contact_id: str) -> MemoryService:
        cfg = MemoryConfig(db_path=str(tmp_path / "mem.db"))
        svc = MemoryService(workspace=tmp_path / "ws", config=cfg)
        mock = MagicMock()
        mock.known_jids = {full_jid: contact_id}
        svc.set_contacts(mock)
        return svc

    def test_resolves_exact_match(self, tmp_path: Path) -> None:
        svc = self._service_with_jid(tmp_path, "491786@s.whatsapp.net", "c-1")
        assert svc._resolve_contact_id("491786@s.whatsapp.net") == "c-1"

    def test_resolves_stripped_user_token_via_whatsapp_suffix(self, tmp_path: Path) -> None:
        svc = self._service_with_jid(tmp_path, "491786@s.whatsapp.net", "c-1")
        assert svc._resolve_contact_id("491786") == "c-1"

    def test_resolves_stripped_user_token_via_lid_suffix(self, tmp_path: Path) -> None:
        svc = self._service_with_jid(tmp_path, "263036883452098@lid", "c-lid")
        assert svc._resolve_contact_id("263036883452098") == "c-lid"

    def test_returns_none_for_unknown_sender(self, tmp_path: Path) -> None:
        svc = self._service_with_jid(tmp_path, "491786@s.whatsapp.net", "c-1")
        assert svc._resolve_contact_id("999999") is None


class TestOwnerOnlyPreferenceGate:
    @staticmethod
    def _strict_service_with_contact(
        tmp_path: Path, jid: str, contact_id: str
    ) -> MemoryService:
        cfg = MemoryConfig(
            db_path=str(tmp_path / "mem.db"),
            acl=MemoryAclConfig(owner_only_preference=True),
        )
        svc = MemoryService(workspace=tmp_path / "ws", config=cfg)
        mock_contacts = MagicMock()
        mock_contacts.known_jids = {jid: contact_id}
        svc.set_contacts(mock_contacts)
        return svc

    def test_person_profile_bypasses_owner_only_gate(self, tmp_path: Path) -> None:
        jid = "491786@s.whatsapp.net"
        contact_id = "frank-uuid-001"
        svc = self._strict_service_with_contact(tmp_path, jid, contact_id)

        candidate = ExtractedCandidate(
            sector="semantic",
            kind="person_profile",
            content="Frank: is a doctor",
            salience=0.9,
            confidence=0.9,
        )
        persisted = svc._persist_candidate(
            channel="whatsapp",
            chat_id="group-a",
            sender_id=jid,
            role="user",
            source_message_id=None,
            candidate=candidate,
        )
        assert persisted is True

        hits = svc.store.search_lexical(
            workspace_id=svc.workspace_id,
            query="Frank doctor",
            scope_keys=[MemoryService.contact_scope_key(contact_id)],
            limit=5,
        )
        assert any("Frank" in h.entry.content for h in hits)

    def test_semantic_preference_still_gated_for_non_owner(self, tmp_path: Path) -> None:
        jid = "491786@s.whatsapp.net"
        svc = self._strict_service_with_contact(tmp_path, jid, "frank-uuid-001")

        candidate = ExtractedCandidate(
            sector="semantic",
            kind="preference",
            content="prefers dark mode",
            salience=0.7,
            confidence=0.8,
        )
        persisted = svc._persist_candidate(
            channel="whatsapp",
            chat_id="group-a",
            sender_id=jid,
            role="user",
            source_message_id=None,
            candidate=candidate,
        )
        assert persisted is False


class TestRecallContactScope:
    def test_person_profile_recalled_from_different_group(self, tmp_path: Path) -> None:
        jid = "491786@s.whatsapp.net"
        contact_id = "frank-uuid-001"
        svc = _service_with_contact(tmp_path, jid, contact_id)

        candidate = ExtractedCandidate(
            sector="semantic",
            kind="person_profile",
            content="Frank: is a doctor",
            salience=0.9,
            confidence=0.9,
        )
        svc._persist_candidate(
            channel="whatsapp",
            chat_id="group-a",
            sender_id=jid,
            role="user",
            source_message_id=None,
            candidate=candidate,
        )

        # Recall from group-b with Frank as sender — should surface group-a fact
        hits = svc.recall_for_event(
            channel="whatsapp",
            chat_id="group-b",
            sender_id=jid,
            query="Frank doctor",
        )
        assert any("Frank" in h.entry.content for h in hits)

    def test_recall_includes_reply_to_contact_scope(self, tmp_path: Path) -> None:
        frank_jid = "491786@s.whatsapp.net"
        contact_id = "frank-uuid-001"
        svc = _service_with_contact(tmp_path, frank_jid, contact_id)

        candidate = ExtractedCandidate(
            sector="semantic",
            kind="person_profile",
            content="Frank: is a doctor",
            salience=0.9,
            confidence=0.9,
        )
        svc._persist_candidate(
            channel="whatsapp",
            chat_id="group-a",
            sender_id=frank_jid,
            role="user",
            source_message_id=None,
            candidate=candidate,
        )

        # Owner replies to Frank — should surface Frank's profile
        hits = svc.recall_for_event(
            channel="whatsapp",
            chat_id="group-b",
            sender_id="owner@wa",
            query="what does Frank do",
            reply_to_jid=frank_jid,
        )
        assert any("Frank" in h.entry.content for h in hits)

    def test_recall_no_crash_when_sender_and_reply_same_contact(self, tmp_path: Path) -> None:
        jid = "491786@s.whatsapp.net"
        contact_id = "frank-uuid-001"
        svc = _service_with_contact(tmp_path, jid, contact_id)

        hits = svc.recall_for_event(
            channel="whatsapp",
            chat_id="group-a",
            sender_id=jid,
            query="anything",
            reply_to_jid=jid,
        )
        assert isinstance(hits, list)
