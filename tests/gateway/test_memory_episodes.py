"""Phase 2 / Task 3: source-preserving episode consolidation.

An episode is a *derived* statement about closed context.  It is never independent human
evidence: it carries the model and prompt version, an uncertainty, and the complete set of
covered source and statement revisions.  The same source never counts twice, a revocation
or correction makes the episode stale before the next read, rebuilding keeps the prior
version for audit, and an episode can never be disclosed more broadly than every one of
its sources permits.

Offline and synthetic: temporary database, deterministic summarizer, no network.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import pytest
from yeoman_gateway.knowledge.api import KnowledgeService
from yeoman_gateway.knowledge.models import (
    DAY_MS,
    EPISODE_CLOSURE_MS,
    EPISODE_STATUSES,
    PersonLinkCandidate,
    SourceRef,
    StatementCandidate,
    TrustedAdminContext,
    TrustedCaptureContext,
    TrustedReadContext,
)

WORKSPACE_ID = "episode-workspace"
OWNER = "whatsapp:4910000000001"
TOM = "whatsapp:4910000000002"
ALEX = "whatsapp:4910000000003"
CHAT = "group-a"
OTHER_CHAT = "group-b"
SCOPE = f"channel:whatsapp:chat:{CHAT}"


@dataclass
class EpisodeHarness:
    tmp_path: Path

    def __post_init__(self) -> None:
        from tests.gateway.knowledge.conftest import KnowledgeHarness

        self.history = KnowledgeHarness(self.tmp_path / "history")
        self.clock = self.history.clock
        self.authority = self.history.authority
        self.policy = self.history.policy
        self.service: KnowledgeService = self.history.service
        self._counter = 0
        self._people: dict[str, str] = {}

    def close(self) -> None:
        self.history.close()

    # ── people ───────────────────────────────────────────────────────────────

    def person(self, principal: str) -> str:
        if principal in self._people:
            return self._people[principal]
        person_id = self.history.person_for(principal, principal)
        self._people[principal] = person_id
        return person_id

    # ── sources and statements ───────────────────────────────────────────────

    def source(
        self,
        *,
        chat: str = CHAT,
        author: str = TOM,
        audience: set[str] | None = None,
        when_ms: int | None = None,
        revision: int = 1,
    ) -> SourceRef:
        return self.history.source(
            self.person(author), chat=chat, audience=set(audience) if audience is not None else {author},
            when_ms=when_ms,
        )

    def capture_context(self, *sources: SourceRef) -> TrustedCaptureContext:
        return self.history.capture_context(*sources)

    def capture(
        self,
        text: str,
        source: SourceRef,
        *,
        author: str = TOM,
        valid_until_ms: int | None = None,
    ) -> str:
        result = self.service.capture(
            StatementCandidate(
                content=text,
                sources=(source,),
                people=(
                    PersonLinkCandidate(
                        person_id=self.person(author),
                        role="speaker",
                        source=source,
                        attribution="transport",
                    ),
                ),
                extractor_version="episode-extractor-1",
                confidence=0.5,
                valid_until_ms=valid_until_ms,
            ),
            context=self.capture_context(source),
        )
        assert result.statement_ids, result.rejected
        return result.statement_ids[0]

    def admin_context(self) -> TrustedAdminContext:
        return TrustedAdminContext(
            actor_principal=OWNER,
            policy_revision=self.policy.revision,
            authorization_ref="admin-ref-1",
            owner=True,
        )

    def read_context(self, reader: str = TOM, *, chat: str = CHAT) -> TrustedReadContext:
        members = {reader}
        self.policy.set_members(
            TrustedReadContext(
                principal_id=reader,
                channel="whatsapp",
                chat_id=chat,
                recipient_principals=frozenset(members),
                membership_revision=f"mem-{chat}",
                policy_revision=self.policy.revision,
                purpose="reply",
                now_ms=self.clock.now_ms(),
            ),
            members,
            revision=f"mem-{chat}",
        )
        return TrustedReadContext(
            principal_id=reader,
            channel="whatsapp",
            chat_id=chat,
            recipient_principals=frozenset(members),
            membership_revision=f"mem-{chat}",
            policy_revision=self.policy.revision,
            purpose="reply",
            now_ms=self.clock.now_ms(),
        )

    def summarizer(self, seeds) -> str:
        """Deterministic, offline episode text: an order-independent digest."""
        parts = sorted(f"{seed.statement_id}:{seed.content}" for seed in seeds)
        return " | ".join(parts)

    def build(self, *, chat: str = CHAT, now_ms: int | None = None):
        return self.service.consolidate_episodes(
            scope_key=f"channel:whatsapp:chat:{chat}",
            summarizer=self.summarizer,
            now_ms=self.clock.now_ms() if now_ms is None else now_ms,
            context=self.admin_context(),
        )

    def episodes(self, *, chat: str = CHAT, reader: str = TOM):
        return self.service.episodes(
            scope_key=f"channel:whatsapp:chat:{chat}",
            context=self.read_context(reader, chat=chat),
        )


@pytest.fixture
def h(tmp_path: Path) -> Iterator[EpisodeHarness]:
    harness = EpisodeHarness(tmp_path)
    try:
        yield harness
    finally:
        harness.close()


def _long_ago_ms(h: EpisodeHarness) -> int:
    """Older than the closure boundary by a comfortable margin."""
    return h.clock.now_ms() - (EPISODE_CLOSURE_MS + 30 * DAY_MS)


# ── eligibility ──────────────────────────────────────────────────────────────


def test_only_inactive_closed_context_older_than_the_boundary_is_eligible(
    h: EpisodeHarness,
) -> None:
    old = h.source(when_ms=_long_ago_ms(h))
    recent = h.source()
    old_statement = h.capture("the summer trip was cancelled", old)
    recent_statement = h.capture("the harbour ferry runs daily", recent)

    report = h.build()
    assert report.created == 1

    episode = h.episodes()[0]
    covered = {source.statement_id for source in episode.sources if source.statement_id}
    assert old_statement in covered or covered == {old_statement}
    assert recent_statement not in covered


def test_open_promises_and_future_events_stay_operational(h: EpisodeHarness) -> None:
    old = h.source(when_ms=_long_ago_ms(h))
    open_promise = h.capture("I will send the contract next week", old)
    future = h.capture("the meeting is planned", old, valid_until_ms=h.clock.now_ms() + 10 * DAY_MS)
    closed = h.capture("the old workshop already happened", old)

    report = h.build()
    assert report.created == 1
    covered = {source.statement_id for source in h.episodes()[0].sources if source.statement_id}
    assert closed in covered
    assert open_promise not in covered
    assert future not in covered


def test_nothing_is_consolidated_when_everything_is_still_open(h: EpisodeHarness) -> None:
    recent = h.source()
    h.capture("just said today", recent)
    report = h.build()
    assert report.created == 0
    assert h.episodes() == ()


# ── provenance ───────────────────────────────────────────────────────────────


def test_every_episode_lists_all_covered_source_and_statement_revisions(
    h: EpisodeHarness,
) -> None:
    first = h.source(when_ms=_long_ago_ms(h))
    second = h.source(when_ms=_long_ago_ms(h) + 1000)
    one = h.capture("first closed statement", first)
    two = h.capture("second closed statement", second)

    h.build()
    episode = h.episodes()[0]
    covered = {(source.event_id, source.revision, source.statement_id) for source in episode.sources}
    assert covered == {
        (first.event_id, first.revision, one),
        (second.event_id, second.revision, two),
    }
    assert episode.source_count == 2


def test_one_source_is_not_counted_twice(h: EpisodeHarness) -> None:
    source = h.source(when_ms=_long_ago_ms(h))
    context = h.capture_context(source)
    for text in ("statement alpha", "statement beta"):
        h.service.capture(
            StatementCandidate(
                content=text,
                sources=(source,),
                people=(
                    PersonLinkCandidate(
                        person_id=h.person(TOM),
                        role="speaker",
                        source=source,
                        attribution="transport",
                    ),
                ),
                extractor_version="episode-extractor-1",
                confidence=0.5,
            ),
            context=context,
        )

    h.build()
    episode = h.episodes()[0]
    assert episode.source_count == 1
    assert len({(item.event_id, item.revision) for item in episode.sources}) == 1


def test_episode_is_a_derived_statement_not_human_evidence(h: EpisodeHarness) -> None:
    source = h.source(when_ms=_long_ago_ms(h))
    h.capture("the workshop already happened", source)
    h.build()
    episode = h.episodes()[0]
    assert episode.model_version
    assert episode.prompt_version
    assert 0.0 <= episode.uncertainty <= 1.0
    assert episode.derived is True
    assert episode.version == 1


# ── staleness ────────────────────────────────────────────────────────────────


def test_revocation_marks_the_episode_stale_before_the_next_read(h: EpisodeHarness) -> None:
    source = h.source(when_ms=_long_ago_ms(h))
    h.capture("a closed statement that will be withdrawn", source)
    h.build()
    assert h.episodes()
    assert h.episodes()[0].stale is False

    h.service.invalidate_source(source, context=h.capture_context(source))

    after = h.episodes()
    assert after
    assert after[0].stale is True
    assert after[0].reason == "stale_source"


def test_correction_marks_the_episode_stale_before_the_next_read(h: EpisodeHarness) -> None:
    source = h.source(when_ms=_long_ago_ms(h))
    statement_id = h.capture("a closed statement that gets corrected", source)
    h.build()
    assert h.episodes()[0].stale is False

    h.service.correct_statement(
        statement_id,
        StatementCandidate(
            content="the corrected closed statement",
            sources=(source,),
            extractor_version="episode-extractor-1",
            confidence=0.5,
        ),
        expected_source=source,
        context=h.capture_context(source),
    )

    after = h.episodes()
    assert after[0].stale is True


# ── rebuild ──────────────────────────────────────────────────────────────────


def test_rebuilding_is_idempotent(h: EpisodeHarness) -> None:
    source = h.source(when_ms=_long_ago_ms(h))
    h.capture("a closed statement to rebuild", source)

    first = h.build()
    assert first.created == 1
    second = h.build()
    assert second.created == 0
    assert second.reused == 1

    rows = h.service._store.query(  # noqa: SLF001 - white-box version assertion
        "SELECT episode_id, version, status FROM knowledge_episodes"
    )
    assert len(rows) == 1
    assert int(rows[0]["version"]) == 1


def test_rebuilding_preserves_the_prior_version_for_audit(h: EpisodeHarness) -> None:
    source = h.source(when_ms=_long_ago_ms(h))
    h.capture("the first closed statement", source)
    h.build()

    later = h.source(when_ms=_long_ago_ms(h) + 5000)
    h.capture("a second closed statement", later)
    report = h.build()
    assert report.created == 1

    rows = h.service._store.query(  # noqa: SLF001 - white-box audit assertion
        "SELECT episode_id, version, status, supersedes FROM knowledge_episodes"
        " ORDER BY version"
    )
    assert [int(row["version"]) for row in rows] == [1, 2]
    assert str(rows[0]["status"]) == "superseded"
    assert str(rows[1]["supersedes"]) == str(rows[0]["episode_id"])
    # The superseded version stays readable for audit.
    assert h.service.episode(str(rows[0]["episode_id"]), context=h.read_context()).reason == (
        "superseded"
    )


# ── disclosure ───────────────────────────────────────────────────────────────


def test_episode_disclosure_is_no_broader_than_its_sources(h: EpisodeHarness) -> None:
    source = h.source(when_ms=_long_ago_ms(h), audience={TOM})
    h.capture("closed and only for tom", source)
    h.build()

    assert h.episodes(reader=TOM)
    assert h.episodes(reader=ALEX)[0].reason in ("empty", "not_permitted")
    assert h.episodes(reader=ALEX)[0].text == ""


def test_episode_of_another_chat_is_never_readable(h: EpisodeHarness) -> None:
    source = h.source(chat=OTHER_CHAT, when_ms=_long_ago_ms(h))
    h.capture("closed context of another chat", source)
    h.build(chat=OTHER_CHAT)

    assert h.episodes(chat=OTHER_CHAT, reader=TOM)
    cross = h.service.episodes(
        scope_key=f"channel:whatsapp:chat:{OTHER_CHAT}", context=h.read_context(TOM, chat=CHAT)
    )
    assert cross == ()


# ── PDF ──────────────────────────────────────────────────────────────────────


def test_passive_pdf_reference_is_ignored(h: EpisodeHarness) -> None:
    source = h.source(when_ms=_long_ago_ms(h))
    h.capture("see the attached report", source)
    report = h.build()
    assert report.created == 1
    # A bare media reference is not a source of its own.
    assert all(item.statement_id for item in h.episodes()[0].sources)


def test_episode_constant_contract() -> None:
    assert 55 * DAY_MS <= EPISODE_CLOSURE_MS <= 65 * DAY_MS
    assert set(EPISODE_STATUSES) == {"active", "superseded", "stale"}
