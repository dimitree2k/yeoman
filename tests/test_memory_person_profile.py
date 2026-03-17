"""Tests for person-profile contact-scope memory storage."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from yeoman.config.schema import MemoryAclConfig, MemoryConfig
from yeoman.memory.extractor import ExtractedCandidate
from yeoman.memory.service import MemoryService


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
