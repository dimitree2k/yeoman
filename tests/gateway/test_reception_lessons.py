"""Reception lessons: file cap, proposer input, admission, approval and trial watch."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from yeoman_gateway.ipc.gateway_socket import GatewaySocket
from yeoman_gateway.knowledge._person_notes import PersonNoteStore
from yeoman_gateway.knowledge._store import KnowledgeStore
from yeoman_gateway.knowledge.models import PersonNoteLine, PersonNoteSeed
from yeoman_gateway.reception.lessons import (
    LESSONS_HEADER,
    Evidence,
    LessonItem,
    LessonProposer,
    LessonService,
    LessonsJob,
    SituationStat,
    collect_proposer_input,
    compose_persona_with_lessons,
    lessons_path,
    load_lessons_text,
    parse_proposals,
    render_lessons_file,
)
from yeoman_gateway.reception.stance import LabelRow
from yeoman_gateway.reception.store import SentimentStore

CHAT = "120363000000000081@g.us"
BASE = 1_700_000_000
DAY_MS = 86_400_000


def _stat(key: str) -> SituationStat:
    return SituationStat(
        key=key,
        path="direct_reply",
        action_type="unknown",
        addressed=True,
        target_elsewhere=False,
        after_pushback=False,
        outputs=2,
        replies=2,
        negative=1,
        people=1,
    )


def _evidence(opaque: str, *, people: set[str], key: str = "k1") -> Evidence:
    return Evidence(
        opaque_id=opaque,
        message_id=f"m-{opaque}",
        text="Arvid text",
        stat_key=key,
        people=frozenset(people),
    )


def _answer(*items: dict[str, Any]) -> str:
    return json.dumps({"lessons": list(items)})


def _valid(**overrides: Any) -> dict[str, Any]:
    item = {
        "situation": "Wenn jemand stichelt",
        "effect": "Arvid wurde länger",
        "change": "Kurz antworten oder schweigen",
        "evidence_ids": ["e1", "e2"],
    }
    item.update(overrides)
    return item


def test_lessons_file_header_and_cap(tmp_path: Path) -> None:
    items = [LessonItem(f"Situation {index}", "E" * 180, "C" * 180, ("e1",)) for index in range(8)]
    text = render_lessons_file(items)
    assert text.startswith(LESSONS_HEADER)
    path = lessons_path(tmp_path, CHAT)
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    loaded = load_lessons_text(tmp_path, CHAT)
    assert loaded.startswith(LESSONS_HEADER)
    assert loaded.count("- Situation:") <= 5
    assert len(loaded) <= 1_500
    assert "Lessons from reception" in (compose_persona_with_lessons("P", tmp_path, CHAT) or "")
    path.unlink()
    assert load_lessons_text(tmp_path, CHAT) == ""


def test_proposer_input_never_contains_human_text() -> None:
    rows = [
        {
            "message_id": "a1",
            "native_message_id": "n1",
            "direction": "out",
            "text": "Arvid synthetic answer",
            "current_text": "Arvid synthetic answer",
            "sender_contact_id": "arvid",
        },
        {
            "message_id": "h1",
            "native_message_id": "hn1",
            "direction": "in",
            "text": "HUMAN_MARKER_SECRET",
            "sender_contact_id": "person-1",
            "reply_to_native_id": "n1",
        },
        {
            "message_id": "a2",
            "native_message_id": "n2",
            "direction": "out",
            "text": "Arvid second answer",
            "sender_contact_id": "arvid",
            "reply_to_native_id": "hn1",
        },
    ]
    labels = [
        LabelRow(
            chat_id=CHAT,
            iso_week="2026-W01",
            arvid_message_id="a1",
            message_id="h1",
            responds_to_arvid="yes",
            stance="hostile",
            signals=(),
            confidence=0.9,
            model="m",
            prompt_version="v",
            labeled_at_ms=BASE * 1000,
        )
    ]
    built = collect_proposer_input(
        chat_id=CHAT,
        week=SimpleNamespace(
            label="2026-W01", start_ms=BASE * 1000, end_ms=(BASE + 7 * 86_400) * 1000
        ),
        labels=labels,
        outbound={"n1": {"path": "direct_reply", "action_type": "unknown", "sent_ms": BASE * 1000}},
        rows=rows,
    )
    payload = json.dumps(built.payload(), ensure_ascii=False)
    assert "HUMAN_MARKER_SECRET" not in payload
    assert "Arvid synthetic answer" in payload
    assert built.stats and built.evidence


def test_admission_needs_two_people_or_three_incidents() -> None:
    stats = [_stat("k1"), _stat("k2"), _stat("k3")]
    evidence = [
        _evidence("e1", people={"p1"}, key="k1"),
        _evidence("e2", people={"p2"}, key="k1"),
        _evidence("e3", people={"p1"}, key="k2"),
        _evidence("e4", people={"p1"}, key="k3"),
    ]
    admitted = parse_proposals(
        _answer(_valid()),
        evidence=evidence,
        stats=stats,
        member_names=(),
    )
    assert len(admitted.admitted) == 1
    rejected = parse_proposals(
        _answer(_valid(evidence_ids=["e3", "e4"])),
        evidence=evidence,
        stats=stats,
        member_names=(),
    )
    assert rejected.admitted == ()
    assert rejected.rejected[0]["reason"] == "insufficient_evidence"
    unresolved = parse_proposals(
        _answer(_valid(evidence_ids=["missing"])),
        evidence=evidence,
        stats=stats,
        member_names=(),
    )
    assert unresolved.admitted == ()
    assert unresolved.rejected[0]["reason"] == "unresolved_evidence"


def test_name_rejection_and_field_validation() -> None:
    stats = [_stat("k1"), _stat("k2")]
    evidence = [
        _evidence("e1", people={"p1"}, key="k1"),
        _evidence("e2", people={"p2"}, key="k2"),
    ]
    named = parse_proposals(
        _answer(_valid(effect="Wie Tom es macht")),
        evidence=evidence,
        stats=stats,
        member_names=("Tom",),
    )
    assert named.admitted == ()
    assert named.rejected[0]["reason"] == "member_name"
    too_long = parse_proposals(
        _answer(_valid(change="x" * 201)),
        evidence=evidence,
        stats=stats,
        member_names=(),
    )
    assert too_long.admitted == ()
    assert too_long.rejected[0]["reason"] == "invalid_field"


def test_sentiment_lesson_schema_migrates_from_version_one(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "ops" / "sentiment.db"
    path.parent.mkdir(parents=True)
    store = SentimentStore(path)
    store._db()
    store.close()
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE lesson_proposals")
        db.execute("DROP TABLE lesson_versions")
        db.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
    reopened = SentimentStore(path)
    try:
        tables = {
            str(row[0])
            for row in reopened._db().execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"lesson_proposals", "lesson_versions"} <= tables
        proposal = reopened.create_lesson_proposal(
            chat_id=CHAT,
            created_ms=1,
            items=_items("migriert"),
            diff_text="+x",
        )
        assert proposal.status == "open"
    finally:
        reopened.close()


def _git(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)


def _items(change: str) -> list[dict[str, Any]]:
    return [
        {
            "situation": "Situation",
            "effect": "Wirkung",
            "change": change,
            "evidence_ids": ["e1", "e2"],
        }
    ]


def test_proposal_accept_reject_rollback_with_git(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    workspace = runtime / "workspace"
    runtime.mkdir()
    _git(runtime)
    store = SentimentStore(runtime / "ops" / "sentiment.db")
    try:
        first = store.create_lesson_proposal(
            chat_id=CHAT,
            created_ms=BASE * 1000,
            items=_items("erste Änderung"),
            diff_text="+erste",
        )
        service = LessonService(store=store, workspace=workspace, runtime_home=runtime)
        accepted = service.accept(first.id, now_ms=(BASE + 1) * 1000)
        assert accepted["status"] == "ok" and accepted["version"] == 1
        path = lessons_path(workspace, CHAT)
        assert "erste Änderung" in path.read_text(encoding="utf-8")
        assert store.latest_lesson_version(CHAT).version == 1
        log = subprocess.run(
            ["git", "-C", str(runtime), "log", "--format=%s"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert "lessons:" in log

        second = store.create_lesson_proposal(
            chat_id=CHAT,
            created_ms=(BASE + 2) * 1000,
            items=_items("zweite Änderung"),
            diff_text="+zweite",
        )
        assert service.accept(second.id, now_ms=(BASE + 2) * 1000)["version"] == 2
        assert "zweite Änderung" in path.read_text(encoding="utf-8")

        rejected = store.create_lesson_proposal(
            chat_id=CHAT,
            created_ms=(BASE + 3) * 1000,
            items=_items("abgelehnt"),
            diff_text="+nein",
        )
        assert service.reject(rejected.id)["status"] == "ok"
        assert "zweite Änderung" in path.read_text(encoding="utf-8")

        rolled = service.rollback(CHAT, version=1, now_ms=(BASE + 4) * 1000)
        assert rolled["status"] == "ok" and rolled["version"] == 3
        assert "erste Änderung" in path.read_text(encoding="utf-8")
    finally:
        store.close()


def test_git_failure_does_not_fail_accept(tmp_path: Path) -> None:
    runtime = tmp_path / "not-a-repo"
    store = SentimentStore(tmp_path / "ops" / "sentiment.db")
    try:
        proposal = store.create_lesson_proposal(
            chat_id=CHAT,
            created_ms=BASE * 1000,
            items=_items("ohne git"),
            diff_text="+x",
        )
        service = LessonService(
            store=store,
            workspace=runtime / "workspace",
            runtime_home=runtime,
        )
        result = service.accept(proposal.id, now_ms=(BASE + 1) * 1000)
        assert result["status"] == "ok" and result["version"] == 1
        assert lessons_path(runtime / "workspace", CHAT).exists()
    finally:
        store.close()


def test_digest_line_reports_active_version_and_trial(tmp_path: Path) -> None:
    store = SentimentStore(tmp_path / "ops" / "sentiment.db")
    try:
        store.record_lesson_version(
            chat_id=CHAT,
            applied_ms=BASE * 1000,
            items=_items("aktiv"),
            source_proposal="p1",
        )
        proposer = LessonProposer(
            store=store,
            history=object(),
            workspace=tmp_path / "workspace",
            runtime_home=tmp_path,
            client_factory=lambda: object(),
            send_owner=lambda text: None,
        )
        job = LessonsJob(proposer=proposer, chats=lambda: (CHAT,))
        assert "v1" in job.digest_line(CHAT)
        assert "Probe" in job.digest_line(CHAT)
        store.set_meta(f"lessons_trial_checked:{CHAT}:1", json.dumps({"result": "kept"}))
        assert "gehalten" in job.digest_line(CHAT)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_trial_week_proposes_rollback(tmp_path: Path) -> None:
    store = SentimentStore(tmp_path / "ops" / "sentiment.db")
    sent: list[str] = []
    try:
        applied = (BASE - 8 * 86_400) * 1000
        store.record_lesson_version(
            chat_id=CHAT,
            applied_ms=applied,
            items=_items("alt"),
            source_proposal="p1",
        )
        before_start = applied - 7 * DAY_MS
        after_start = applied
        for index in range(10):
            store.record_outbound(
                chat_id=CHAT,
                message_id=f"before-{index}",
                sent_ms=before_start + index * 1000,
                path="direct_reply",
            )
        store.record_outbound(
            chat_id=CHAT,
            message_id="after-1",
            sent_ms=after_start + 1000,
            path="direct_reply",
        )
        store.store_label(
            chat_id=CHAT,
            iso_week="2026-W01",
            arvid_message_id="before-1",
            message_id="before-reply",
            responds_to_arvid="yes",
            stance="neutral",
            signals=(),
            confidence=0.9,
            model="m",
            prompt_version="v",
            labeled_at_ms=before_start + 2000,
        )
        store.store_label(
            chat_id=CHAT,
            iso_week="2026-W01",
            arvid_message_id="after-1",
            message_id="after-reply",
            responds_to_arvid="yes",
            stance="hostile",
            signals=(),
            confidence=0.9,
            model="m",
            prompt_version="v",
            labeled_at_ms=after_start + 2000,
        )

        async def send_owner(text: str) -> None:
            sent.append(text)

        proposer = LessonProposer(
            store=store,
            history=object(),
            workspace=tmp_path / "workspace",
            runtime_home=tmp_path,
            client_factory=lambda: object(),
            send_owner=send_owner,
        )
        result = await proposer.propose_trial_rollback(CHAT, now_ms=(BASE + 1) * 1000)
        assert result.state == "done" and result.proposal_id
        proposal = store.lesson_proposal(result.proposal_id)
        assert proposal is not None and proposal.status == "open"
        assert sent and "accept" in sent[0]
    finally:
        store.close()


class _PagedHistory:
    """Week of history rows behind the real ``recent`` limit check (1..500)."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows  # oldest first, like HistoryQueries.recent
        self.limits: list[int] = []

    def recent(self, *, chat_id: str, limit: int, before_id: str | None = None,
               after_ms: int | None = None, until_ms: int | None = None) -> list[Any]:
        from yeoman_gateway.history.queries import _limit

        _limit(limit)
        self.limits.append(limit)
        rows = [
            row for row in self.rows
            if (after_ms is None or row["sent_ms"] > after_ms)
            and (until_ms is None or row["sent_ms"] <= until_ms)
        ]
        if before_id is not None:
            index = next(i for i, row in enumerate(rows) if row["message_id"] == before_id)
            rows = rows[:index]
        return rows[-limit:]

    def terminal(self, person: str) -> str:
        return person

    def contact(self, person: str) -> dict[str, str]:
        return {"display_name": "Early Member" if person == "p-0" else "Later Member"}


@pytest.mark.asyncio
async def test_proposal_read_pages_a_busy_week(tmp_path: Path, monkeypatch) -> None:
    from contextlib import asynccontextmanager

    import yeoman_gateway.history.context as history_context
    import yeoman_gateway.history.queries as history_queries
    from yeoman_gateway.history.group_signals import previous_week

    now_ms = (BASE + 7 * 86_400) * 1000
    week = previous_week(now_ms, tz=None)
    rows = [
        {
            "message_id": f"m-{index:04d}",
            "sent_ms": int(week.start_ms) + index * 1000,
            "direction": "in",
            "sender_contact_id": f"p-{index}",
        }
        for index in range(1_200)
    ]
    history = _PagedHistory(rows)

    @asynccontextmanager
    async def fake_turn(_history: Any):
        yield None

    monkeypatch.setattr(history_context, "history_turn", fake_turn)
    monkeypatch.setattr(history_queries, "HistoryQueries", lambda _snapshot: history)
    store = SentimentStore(tmp_path / "ops" / "sentiment.db")
    try:
        proposer = LessonProposer(
            store=store,
            history=object(),
            workspace=tmp_path / "workspace",
            runtime_home=tmp_path,
            client_factory=lambda: object(),
            send_owner=lambda _text: None,
        )
        built = await proposer._read(CHAT, week)
    finally:
        store.close()
    assert history.limits and max(history.limits) <= 500
    # The oldest message of the week is outside the newest 500 and still read.
    assert "Early Member" in built.member_names


@pytest.mark.asyncio
async def test_ipc_lessons_validation_and_owner_handlers(tmp_path: Path) -> None:
    seen: list[tuple[str, dict[str, Any]]] = []

    async def handler(name: str):
        async def _inner(args: dict[str, Any]) -> dict[str, Any]:
            seen.append((name, dict(args)))
            return {"status": "ok", "name": name}

        return _inner

    socket = GatewaySocket(
        path=tmp_path / "gateway.sock",
        sentiment_lessons_propose_handler=await handler("propose"),
        sentiment_lessons_accept_handler=await handler("accept"),
        sentiment_lessons_reject_handler=await handler("reject"),
        sentiment_lessons_rollback_handler=await handler("rollback"),
    )
    assert (
        await socket._dispatch(
            {
                "cmd": "sentiment_lessons_propose",
                "args": {"chat": CHAT},
            }
        )
    )["name"] == "propose"
    assert (
        await socket._dispatch(
            {
                "cmd": "sentiment_lessons_accept",
                "args": {"proposal_id": "lp-1"},
            }
        )
    )["name"] == "accept"
    assert (
        await socket._dispatch(
            {
                "cmd": "sentiment_lessons_rollback",
                "args": {"chat": CHAT, "version": None},
            }
        )
    )["name"] == "rollback"
    for cmd, args in (
        ("sentiment_lessons_propose", {"chat": "no-jid"}),
        ("sentiment_lessons_propose", {}),
        ("sentiment_lessons_accept", {"proposal_id": "x"}),
        ("sentiment_lessons_reject", {"proposal_id": "lp-1", "x": 1}),
        ("sentiment_lessons_rollback", {"chat": CHAT}),
        ("sentiment_lessons_rollback", {"chat": CHAT, "version": 0}),
    ):
        reply = await socket._dispatch({"cmd": cmd, "args": args})
        assert reply["code"] == "INVALID_LESSONS"
    assert len(seen) == 3


def test_cli_show_and_stance_output(tmp_path: Path, monkeypatch) -> None:
    from typer.testing import CliRunner
    from yeoman_gateway.cli import sentiment_commands as commands
    from yeoman_gateway.cli.core import app

    store = SentimentStore(tmp_path / "ops" / "sentiment.db")
    knowledge = KnowledgeStore(tmp_path / "knowledge.db")
    try:
        proposal = store.create_lesson_proposal(
            chat_id=CHAT,
            created_ms=BASE * 1000,
            items=_items("sichtbare Änderung"),
            diff_text="+sichtbar",
        )
        store.record_lesson_version(
            chat_id=CHAT,
            applied_ms=BASE * 1000,
            items=_items("sichtbare Änderung"),
            source_proposal=proposal.id,
        )
        store.set_lesson_proposal_status(proposal.id, "accepted")
        notes = PersonNoteStore(knowledge, workspace_id="default")
        notes.store_note(
            PersonNoteSeed(
                channel="whatsapp",
                chat_id=CHAT,
                person_id="p",
                input_hash="h",
                input_cut_ms=BASE * 1000,
                lines=(
                    PersonNoteLine(
                        kind="stance",
                        text="Tom: often hostile toward Arvid (3 of 5)",
                        author="Tom",
                    ),
                ),
                sources=(("message", "m1", BASE * 1000),),
                family="stance",
            )
        )
        monkeypatch.setattr(commands, "_lessons_chat_id", lambda chat: CHAT)
        monkeypatch.setattr(commands, "_store_read_only", lambda: store)
        result = CliRunner().invoke(app, ["sentiment", "lessons", "show", CHAT])
        assert result.exit_code == 0, result.output
        assert "sichtbare Änderung" in result.output
        assert "offene" not in result.output.lower()

        monkeypatch.setattr(commands, "_policy_tuned_chat_maps", lambda: ({CHAT: "Gruppe"}, [CHAT]))
        import yeoman_shared.config.loader as loader

        monkeypatch.setattr(
            loader,
            "load_config",
            lambda: SimpleNamespace(
                knowledge=SimpleNamespace(resolved_db_path=tmp_path / "knowledge.db")
            ),
        )
        stance = CliRunner().invoke(app, ["sentiment", "stance", "--chat", CHAT])
        assert stance.exit_code == 0, stance.output
        assert "Tom" in stance.output
        assert "491510000001" not in stance.output
    finally:
        knowledge.close()
        store.close()
