"""Weekly group reaction digest (Spec B, steps B1-B2): call, validation, storage, job.

Synthetic and offline: projected synthetic history, a prepared empty schema-3 Knowledge
copy, and a fake model client. Nothing reads a runtime path.
"""

from __future__ import annotations

import json
from datetime import UTC
from pathlib import Path
from typing import Any

import pytest
from loguru import logger
from yeoman_gateway.history.group_signals import compute_signals, iso_week, week_moment_ms
from yeoman_gateway.history.live import HistoryPaused
from yeoman_gateway.knowledge.api import open_knowledge_store
from yeoman_gateway.knowledge.authority import EvidenceAudience, FakePolicyAuthority
from yeoman_gateway.knowledge.runtime import RuntimeKnowledgeSources
from yeoman_gateway.processing import group_digest as digest_module
from yeoman_gateway.processing.group_digest import (
    HISTORY_PAUSE_DELAY_MS,
    MAX_RENDERED_CHARS,
    RETRY_BACKOFF_MS,
    STALE_RUNNING_MS,
    DigestBounds,
    DigestInvalidError,
    GroupDigestJob,
    GroupDigestRunner,
    build_digest_input,
    digest_messages,
    render_digest,
    validate_digest,
)
from yeoman_gateway.processing.model_route import RouteReply, RouteUnavailableError

from tests.gateway.history_test_helpers import ProjectedHistory, prepare_empty_history_knowledge

CHAT = "120363000000000077@g.us"
QUIET = "120363000000000078@g.us"
MIN = 60_000
WEEK = iso_week("2026-W40", tz=UTC)
NEXT_MONDAY_0400 = week_moment_ms("2026-W41", hours=4, tz=UTC)
BOUNDS = DigestBounds(lull_ms=25 * MIN, burst_n=4, burst_ms=15 * MIN)
GOOD = {
    "goes_down_well": ["Short dry jokes about markets", "Charts with one line of context"],
    "engages": ["Rate debates, friendly but long", "Crypto, heated"],
    "arvid_effect": ["Short answers get follow-ups; long ones end threads"],
    "confidence": "medium",
}


def _phone(index: int) -> str:
    return f"49151000{index:04d}"


class FakeClient:
    """Counts calls; answers from a queue (a string, an exception, or a reply)."""

    model = "fake/digest-model"
    structured_output = "json_schema_strict"

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers) or [json.dumps(GOOD)]
        self.calls: list[list[dict[str, str]]] = []

    async def chat_with_usage(self, messages, **kwargs):  # noqa: ANN001, ANN003
        self.calls.append(messages)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, RouteReply):
            return answer
        return RouteReply(content=answer, model=self.model, usage={"prompt_tokens": 1})


def _busy_week(history: ProjectedHistory, chat: str = CHAT, *, conversations: int = 6) -> None:
    """Enough signal: six two-hour-apart conversations, 1-3 reactions per message."""
    base = WEEK.start_ms // 1000 + 9 * 3600
    for conversation in range(conversations):
        for line in range(5):
            mid = f"{chat[-4:]}-c{conversation}-m{line}"
            sender = f"{_phone(line % 3 + 1)}@s.whatsapp.net"
            history.add_message(
                chat_id=chat, message_id=mid, text=f"synthetic topic {conversation} line {line}",
                timestamp=base + conversation * 7200 + line * 60, sender_id=sender,
                participant=sender,
            )
            for reactor in range(line % 3 + 1):
                history.append(
                    "reaction", chat_id=chat, timestamp=base + conversation * 7200 + line * 60 + 30,
                    emoji="😂", removed=False,
                    senderId=f"{_phone(900 + reactor)}@s.whatsapp.net", targetMessageId=mid,
                )
    history.add_message(chat_id=chat, message_id=f"{chat[-4:]}-arvid", text="synthetic arvid note",
                        timestamp=base + 120, direction="out")
    history.add_contact(identifiers=[f"{_phone(1)}@s.whatsapp.net"], name="Bertram Example",
                        timestamp=base - 3600)


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.history = ProjectedHistory(tmp_path / "history")
        self.knowledge = open_knowledge_store(
            prepare_empty_history_knowledge(tmp_path),
            workspace_id="digest-test",
            source_authority=RuntimeKnowledgeSources(),
            policy_authority=FakePolicyAuthority(),
        )
        self.client = FakeClient()
        self.route_ok = True

    def factory(self) -> Any:
        if not self.route_ok:
            raise RouteUnavailableError("models.routes missing 'participation.digest'")
        return self.client

    def runner(self, **kwargs: Any) -> GroupDigestRunner:
        return GroupDigestRunner(
            knowledge=self.knowledge, history=self.history, bounds=BOUNDS,
            client_factory=self.factory, tz=UTC, **kwargs,
        )

    def job(self, chats=(CHAT,), **kwargs: Any) -> GroupDigestJob:
        return GroupDigestJob(runner=self.runner(), knowledge=self.knowledge,
                              chats=lambda: chats, **kwargs)

    def close(self) -> None:
        self.history.close()
        self.knowledge.close()


@pytest.fixture
def h(tmp_path: Path):
    harness = Harness(tmp_path)
    try:
        yield harness
    finally:
        harness.close()


# ── validation ───────────────────────────────────────────────────────────────


def test_valid_output_renders_within_the_limit() -> None:
    content, rendered = validate_digest("```json\n" + json.dumps(GOOD) + "\n```")
    assert content["confidence"] == "medium"
    assert rendered.splitlines()[0] == (
        "Goes down well: Short dry jokes about markets; Charts with one line of context"
    )
    assert rendered.endswith("Confidence: medium")
    assert len(rendered) <= MAX_RENDERED_CHARS
    empty = render_digest({"goes_down_well": [], "engages": [], "arvid_effect": [],
                           "confidence": "low"})
    assert empty.startswith("Goes down well: unclear")


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"goes_down_well": ["a", "b", "c", "d"]}, "too_many_bullets"),
        ({"engages": ["x" * 161]}, "bullet_too_long"),
        ({"engages": ["   "]}, "empty_bullet"),
        ({"confidence": "certain"}, "schema_mismatch"),
        ({"arvid_effect": "not a list"}, "schema_mismatch"),
        ({"extra": ["field"]}, "schema_mismatch"),
        (
            {key: ["y" * 150] * 3 for key in ("goes_down_well", "engages", "arvid_effect")},
            "rendered_too_long",
        ),
    ],
)
def test_schema_and_length_violations_are_rejected(change: dict, reason: str) -> None:
    with pytest.raises(DigestInvalidError) as caught:
        validate_digest(json.dumps({**GOOD, **change}))
    assert caught.value.reason == reason


def test_missing_field_and_bad_json_are_rejected() -> None:
    partial = {key: value for key, value in GOOD.items() if key != "engages"}
    for raw, reason in ((json.dumps(partial), "schema_mismatch"), ("not json", "invalid_json"),
                        ("[]", "schema_mismatch")):
        with pytest.raises(DigestInvalidError) as caught:
            validate_digest(raw)
        assert caught.value.reason == reason


@pytest.mark.parametrize(
    "bullet",
    ["Bertram Example posts the best charts", "bertram's charts land well",
     "Jokes by EXAMPLE work"],
)
def test_participant_names_are_rejected(bullet: str) -> None:
    with pytest.raises(DigestInvalidError) as caught:
        validate_digest(json.dumps({**GOOD, "engages": [bullet]}),
                        names=("Bertram Example", "Al"))
    assert caught.value.reason == "names_participant"


def test_short_tokens_inside_words_and_arvid_are_not_names() -> None:
    validate_digest(json.dumps({**GOOD, "engages": ["Already long talks about Arvid's takes"]}),
                    names=("Al", "Arvid Bot"))


@pytest.mark.parametrize(
    "bullet",
    ["Reply to +49 151 0000 0001 often", "ask 01510000001 for charts", "call 4930123456789"],
)
def test_phone_numbers_are_rejected(bullet: str) -> None:
    with pytest.raises(DigestInvalidError) as caught:
        validate_digest(json.dumps({**GOOD, "goes_down_well": [bullet]}),
                        phones=(_phone(1),))
    assert caught.value.reason == "contains_phone"


def test_ordinary_numbers_pass() -> None:
    validate_digest(json.dumps({**GOOD, "goes_down_well": ["Posts before 09:30 and 2026 outlooks"]}),
                    phones=(_phone(1),))


# ── model input ──────────────────────────────────────────────────────────────


def _rows_with_reactions(count: int) -> tuple[list[dict], list[dict]]:
    rows, reactions = [], []
    for index in range(count):
        rows.append({
            "message_id": f"h-{index}", "native_message_id": f"n-{index}", "chat_id": CHAT,
            "sent_ms": WEEK.start_ms + index * 60 * MIN, "direction": "in",
            "sender_contact_id": f"c-{index % 4}", "current_text": f"msg{index} " + "z" * 300,
        })
        for actor in range(count - index):  # earlier messages get more reactors
            reactions.append({"event_id": f"e{index}-{actor}", "target_message_id": f"h-{index}",
                              "actor_contact_id": f"x{actor}", "emoji": "👍",
                              "occurred_ms": WEEK.start_ms + index * 60 * MIN + 1})
    return rows, reactions


def test_input_is_cut_by_rank_never_mid_message() -> None:
    rows, reactions = _rows_with_reactions(20)
    signals = compute_signals(rows, reactions, chat_id=CHAT, week=WEEK, lull_ms=BOUNDS.lull_ms,
                              burst_n=BOUNDS.burst_n, burst_ms=BOUNDS.burst_ms, tz=UTC)
    full = build_digest_input(signals)
    assert len(full.payload["top_reacted_messages"]) == 15
    assert full.dropped == 0
    small = build_digest_input(signals, max_input_tokens=2_000)
    assert small.estimated_tokens <= 2_000
    assert small.dropped > 0
    kept = small.payload["top_reacted_messages"]
    assert [item["rank"] for item in kept] == list(range(1, len(kept) + 1))
    assert all(len(item["excerpt"]) == 200 for item in kept)  # whole excerpts only
    approval_ids = [mid for mid, role in small.sources if role == "approval"]
    assert approval_ids == [f"h-{index}" for index in range(len(kept))]
    text = digest_messages(small.payload)[1]["content"]
    assert "c-1" not in text and "h-0" not in text  # no sender or message ids for the model


# ── one run ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_makes_one_call_and_stores_with_sources(h: Harness) -> None:
    _busy_week(h.history)
    outcome = await h.runner().run(CHAT, WEEK)
    assert outcome.state == "done" and outcome.model_calls == 1
    assert len(h.client.calls) == 1
    prompt = h.client.calls[0][1]["content"]
    assert "Bertram" not in prompt and _phone(1) not in prompt
    digest = outcome.digest
    assert digest.week == "2026-W40" and digest.version == 1 and digest.status == "active"
    assert digest.prompt_version == "group-digest-v2"
    assert digest.model_version == "fake/digest-model"
    assert {role for _mid, role in digest.sources} == {"approval", "engagement", "arvid"}
    assert digest.signals["reactions"] == outcome.signals.reactions
    assert "synthetic topic" not in json.dumps(digest.signals)
    assert (digest.window_start_ms, digest.window_end_ms) == (WEEK.start_ms, WEEK.end_ms)


@pytest.mark.asyncio
async def test_no_call_below_the_minimum(h: Harness) -> None:
    _busy_week(h.history, conversations=4)  # four conversations: below five
    outcome = await h.runner().run(CHAT, WEEK)
    assert (outcome.state, outcome.reason) == ("skipped", "insufficient_signal")
    assert h.client.calls == []
    assert h.knowledge.group_digests(chat_id=CHAT) == ()


@pytest.mark.asyncio
async def test_names_from_history_reject_the_note_and_store_nothing(h: Harness) -> None:
    _busy_week(h.history)
    h.client = FakeClient(json.dumps({**GOOD, "engages": ["Bertram starts the rate debates"]}))
    outcome = await h.runner().run(CHAT, WEEK)
    assert (outcome.state, outcome.reason) == ("failed", "names_participant")
    assert h.knowledge.group_digests(chat_id=CHAT) == ()


@pytest.mark.asyncio
async def test_too_long_note_gets_one_shortening_retry(h: Harness) -> None:
    _busy_week(h.history)
    long_note = json.dumps({**GOOD, "engages": ["y" * 150, "z" * 150, "w" * 150]})
    h.client = FakeClient(long_note, json.dumps(GOOD))
    outcome = await h.runner().run(CHAT, WEEK)
    assert outcome.state == "done" and outcome.model_calls == 2
    assert len(h.client.calls) == 2
    retry = h.client.calls[1]
    assert retry[-2] == {"role": "assistant", "content": long_note}
    assert "rendered_too_long" in retry[-1]["content"] and retry[-1]["role"] == "user"
    assert len(h.knowledge.group_digests(chat_id=CHAT)) == 1


@pytest.mark.asyncio
async def test_shortening_retry_happens_once_and_only_for_length(h: Harness) -> None:
    _busy_week(h.history)
    long_note = json.dumps({**GOOD, "engages": ["y" * 150, "z" * 150, "w" * 150]})
    h.client = FakeClient(long_note, long_note, json.dumps(GOOD))
    outcome = await h.runner().run(CHAT, WEEK)
    assert (outcome.state, outcome.reason, outcome.model_calls) == (
        "failed", "rendered_too_long", 2
    )
    assert len(h.client.calls) == 2
    h.client = FakeClient("not json", json.dumps(GOOD))
    outcome = await h.runner().run(CHAT, WEEK)
    assert (outcome.reason, outcome.model_calls) == ("invalid_json", 1)
    assert len(h.client.calls) == 1
    assert h.knowledge.group_digests(chat_id=CHAT) == ()


# ── storage ──────────────────────────────────────────────────────────────────


def _store(h: Harness, week: str, sources=(("m-1", "approval"),), chat: str = CHAT):
    content, rendered = validate_digest(json.dumps(GOOD))
    target = iso_week(week, tz=UTC)
    return h.knowledge.store_group_digest(
        chat_id=chat, week=week, content=content, rendered=rendered, model_version="m",
        prompt_version="p", confidence="medium", signals={"reactions": 21},
        window_start_ms=target.start_ms, window_end_ms=target.end_ms, sources=sources,
    )


def test_new_week_supersedes_and_old_versions_stay_readable(h: Harness) -> None:
    first = _store(h, "2026-W40")
    second = _store(h, "2026-W41")
    other = _store(h, "2026-W41", chat=QUIET)
    assert (second.version, second.supersedes) == (2, first.digest_id)
    assert other.version == 1 and other.supersedes is None
    listed = h.knowledge.group_digests(chat_id=CHAT)
    assert [(item.week, item.status, item.reason) for item in listed] == [
        ("2026-W41", "active", "ok"), ("2026-W40", "superseded", "superseded"),
    ]
    assert listed[1].rendered  # audit: the superseded note is still readable
    assert h.knowledge.has_group_digest(chat_id=CHAT, week="2026-W40")
    assert {item.chat_id for item in h.knowledge.group_digests()} == {CHAT, QUIET}


def test_deleted_source_marks_the_digest_stale_before_the_next_read(h: Harness) -> None:
    _store(h, "2026-W40", sources=(("m-1", "approval"), ("m-2", "engagement")))
    present = {"m-1"}
    stale = h.knowledge.group_digests(chat_id=CHAT, source_present=present.__contains__)[0]
    assert (stale.status, stale.reason, stale.rendered) == ("stale", "stale_source", "")
    assert stale.stale_ms is not None
    # Persisted: a later read without the history check still sees it stale.
    assert h.knowledge.group_digests(chat_id=CHAT)[0].status == "stale"


def test_revoked_knowledge_source_marks_the_digest_stale(h: Harness) -> None:
    _store(h, "2026-W40", sources=(("m-9", "arvid"),))
    ledger = h.knowledge.history_source_ledger
    source = ledger.issue(
        message_id="m-9", content_fingerprint="f", channel="whatsapp", chat_id=CHAT,
        author_principal="whatsapp:491510000009", occurred_at_ms=WEEK.start_ms,
        author_contact_id="c-9", audience=EvidenceAudience.author_only(),
    )
    assert h.knowledge.group_digests(chat_id=CHAT)[0].status == "active"
    ledger.revoke(source.event_id, source.revision, reason="source_revoked")
    assert h.knowledge.group_digests(chat_id=CHAT)[0].status == "stale"


def test_digest_jobs_are_not_statement_capture_work(h: Harness) -> None:
    h.knowledge.enqueue_group_digest(chat_id=CHAT, week="2026-W40", now_ms=1)
    assert h.knowledge.due_capture_jobs(now_ms=10**13) == ()
    assert h.knowledge.capture_status(now_ms=10**13)["states"] == {}


# ── weekly job ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_job_enqueues_once_per_chat_and_week_after_monday_0400(h: Harness) -> None:
    _busy_week(h.history)
    job = h.job(chats=(CHAT, QUIET))
    early = await job.tick(now_ms=NEXT_MONDAY_0400 - 1)
    assert early.enqueued == 0 and h.knowledge.group_digest_jobs() == ()
    report = await job.tick(now_ms=NEXT_MONDAY_0400)
    assert report.enqueued == 2
    assert report.outcomes == {"done": 1, "skipped": 1}  # the quiet chat has no signal
    assert len(h.client.calls) == 1
    again = await job.tick(now_ms=NEXT_MONDAY_0400 + 900_000)
    assert again.enqueued == 0 and again.ran == 0
    states = {item.chat_id: (item.week, item.state, item.reason)
              for item in h.knowledge.group_digest_jobs()}
    assert states == {CHAT: ("2026-W40", "done", ""),
                      QUIET: ("2026-W40", "skipped", "insufficient_signal")}
    assert len(h.client.calls) == 1


@pytest.mark.asyncio
async def test_restart_mid_job_is_retried_and_never_doubled(h: Harness) -> None:
    _busy_week(h.history)
    queued = h.knowledge.enqueue_group_digest(chat_id=CHAT, week="2026-W40",
                                              now_ms=NEXT_MONDAY_0400)
    # A first process claimed the job and died before finishing it.
    assert h.knowledge.claim_group_digest_job(queued.job_id, max_attempts=3,
                                              now_ms=NEXT_MONDAY_0400) is not None
    job = h.job()
    soon = await job.tick(now_ms=NEXT_MONDAY_0400 + 60_000)
    assert soon.ran == 0 and soon.recovered == 0  # still within the running lease
    later = await job.tick(now_ms=NEXT_MONDAY_0400 + STALE_RUNNING_MS)
    assert later.recovered == 1 and later.outcomes == {"done": 1}
    record = h.knowledge.group_digest_job(queued.job_id)
    assert (record.state, record.attempts) == ("done", 2)
    # A crash after storing: the next run finds the stored note and makes no call.
    h.knowledge.finish_group_digest_job(queued.job_id, state="failed", reason="interrupted",
                                        now_ms=NEXT_MONDAY_0400)
    record = h.knowledge.group_digest_job(queued.job_id)
    assert record.attempts == 2
    await job.tick(now_ms=NEXT_MONDAY_0400 + STALE_RUNNING_MS + 1)
    assert h.knowledge.group_digest_job(queued.job_id).reason == "already_stored"
    assert len(h.client.calls) == 1
    assert len(h.knowledge.group_digests(chat_id=CHAT)) == 1


@pytest.mark.asyncio
async def test_failures_retry_with_backoff_up_to_three_attempts(h: Harness) -> None:
    _busy_week(h.history)
    h.client = FakeClient(TimeoutError(), "not json", json.dumps({**GOOD, "confidence": "x"}))
    job = h.job()
    moment = NEXT_MONDAY_0400
    await job.tick(now_ms=moment)
    record = h.knowledge.group_digest_jobs()[0]
    assert (record.state, record.reason, record.attempts) == ("failed", "timeout", 1)
    assert record.due_ms == moment + RETRY_BACKOFF_MS
    assert (await job.tick(now_ms=moment + RETRY_BACKOFF_MS - 1)).ran == 0
    moment += RETRY_BACKOFF_MS
    await job.tick(now_ms=moment)
    record = h.knowledge.group_digest_jobs()[0]
    assert (record.reason, record.attempts) == ("invalid_json", 2)
    assert record.due_ms == moment + 2 * RETRY_BACKOFF_MS
    moment += 2 * RETRY_BACKOFF_MS
    await job.tick(now_ms=moment)
    record = h.knowledge.group_digest_jobs()[0]
    assert (record.state, record.reason, record.attempts) == ("failed", "schema_mismatch", 3)
    # Left for the next week: no further attempt, however long we wait.
    assert (await job.tick(now_ms=moment + 10 * RETRY_BACKOFF_MS)).ran == 0
    assert len(h.client.calls) == 3
    assert h.knowledge.group_digests(chat_id=CHAT) == ()


@pytest.mark.asyncio
async def test_history_pause_postpones_without_using_an_attempt(h: Harness) -> None:
    _busy_week(h.history)
    original = h.history.read_turn

    async def paused():
        raise HistoryPaused("startup")

    h.history.read_turn = paused  # type: ignore[method-assign]
    job = h.job()
    await job.tick(now_ms=NEXT_MONDAY_0400)
    record = h.knowledge.group_digest_jobs()[0]
    assert (record.state, record.reason, record.attempts) == ("queued", "history_paused", 0)
    assert record.due_ms == NEXT_MONDAY_0400 + HISTORY_PAUSE_DELAY_MS
    assert h.client.calls == []
    h.history.read_turn = original  # type: ignore[method-assign]
    await job.tick(now_ms=NEXT_MONDAY_0400 + HISTORY_PAUSE_DELAY_MS)
    assert h.knowledge.group_digest_jobs()[0].state == "done"


@pytest.mark.asyncio
async def test_missing_route_logs_once_per_day_and_enqueues_nothing(h: Harness) -> None:
    _busy_week(h.history)
    h.route_ok = False
    job = h.job()
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="WARNING")
    try:
        for offset in (0, 900_000, 1_800_000):
            report = await job.tick(now_ms=NEXT_MONDAY_0400 + offset)
            assert report.route_missing is True
        await job.tick(now_ms=NEXT_MONDAY_0400 + 24 * 60 * MIN)
    finally:
        logger.remove(sink)
    assert sum("group_digest_route_missing" in line for line in lines) == 2  # two days
    assert h.knowledge.group_digest_jobs() == ()
    assert h.client.calls == []


@pytest.mark.asyncio
async def test_maintenance_ticks_the_digest_and_survives_its_failure() -> None:
    from yeoman_gateway.consciousness.participation_maintenance import ParticipationMaintenance

    maintenance = ParticipationMaintenance(ledger=object(), clock_ms=lambda: 123)
    seen: list[int] = []

    class Job:
        async def tick(self, *, now_ms: int) -> str:
            seen.append(now_ms)
            raise RuntimeError("boom")

    assert await maintenance.run_group_digest() is None  # not wired: nothing happens
    maintenance._group_digest = Job()
    assert await maintenance.run_group_digest() is None
    assert seen == [123]


# ── composition ──────────────────────────────────────────────────────────────


def test_production_bootstrap_wires_the_weekly_digest_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from yeoman_gateway.app.bootstrap import build_gateway_runtime
    from yeoman_gateway.bus.queue import MessageBus
    from yeoman_gateway.policy.engine import PolicyEngine
    from yeoman_gateway.policy.schema import PolicyConfig
    from yeoman_shared.config.schema import Config

    from tests.gateway.history_test_helpers import prepare_history_retirement_knowledge

    class NeverProvider:
        def get_default_model(self) -> str:
            return "test/provider"

        async def chat(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("composition must not call an LLM")

    monkeypatch.setattr("yeoman_gateway.processing.model_route.RouteClient",
                        lambda **kwargs: NeverProvider())
    history = ProjectedHistory(tmp_path / "composition-history")
    history.control = lambda *args: None
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path))
    monkeypatch.setattr("yeoman_gateway.app.bootstrap.build_history_projector",
                        lambda *args: history)
    config = Config.model_validate({
        "knowledge": {"enabled": True, "capture_enabled": False,
                      "db_path": str(prepare_history_retirement_knowledge(tmp_path, "kb"))},
        "security": {"enabled": False},
        "personaEvolution": {"enabled": False},
        "consciousness": {"lullSilenceMinutes": 31, "burstThresholdMessages": 5,
                          "burstWindowMinutes": 11},
        "processing": {"enabled": True, "chats": ["whatsapp:group@g.us", "whatsapp:4900@s.whatsapp.net"],
                       "participation": {"enabled": True, "shadow": False,
                                         "judgeRoute": "participation.judge"},
                       "participation_maintenance": {"enabled": True}},
    })
    assert config.processing.participation.group_notes == "off"
    policy = PolicyEngine(PolicyConfig.model_validate({"channels": {"whatsapp": {
        "chats": {"group@g.us": {"participation": {"enabled": True}}}
    }}}), workspace=tmp_path)
    runtime = build_gateway_runtime(config=config, provider=NeverProvider(), policy_engine=policy,
                                    policy_path=None, workspace=tmp_path / "workspace",
                                    bus=MessageBus())
    try:
        maintenance = runtime.participation_maintenance
        assert maintenance is not None
        job = maintenance._group_digest
        assert isinstance(job, GroupDigestJob)
        runner = job.runner
        assert runner._knowledge is not None and runner._history is history
        assert runner._thread_lookup is not None
        assert runner._bounds() == DigestBounds(lull_ms=31 * MIN, burst_n=5, burst_ms=11 * MIN)
        assert list(job._chats()) == ["group@g.us"]  # groups only, policy-active only
        assert runner.client() is runner.client()  # one route client, built on demand
        assert digest_module.DIGEST_ROUTE == "participation.digest"
    finally:
        history.close()
        runtime.speakup_log.close()
        runtime.processing.close()
