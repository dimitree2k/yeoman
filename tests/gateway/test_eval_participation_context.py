"""Offline A/B replay of the participation context (Spec A section 7 step 1).

Synthetic stores and a fake Judge only. Nothing here talks to a provider.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner
from yeoman_gateway.cli.commands import app
from yeoman_gateway.consciousness.log import SpeakupLog
from yeoman_gateway.evaluation.harness import StaticClient
from yeoman_gateway.evaluation.participation_context_ab import (
    FrozenHistory,
    UsageCapture,
    captured_usage,
    chat_subjects,
    label_check,
    label_summary,
    load_opportunities,
    registry_lookup,
    replay,
    resolve_chat,
    summarize,
    write_outputs,
)
from yeoman_gateway.processing.model_route import RouteReply
from yeoman_gateway.processing.participation import ParticipationJudge
from yeoman_gateway.processing.participation_context import (
    ParticipationContextBounds,
    ParticipationContextBuilder,
)
from yeoman_gateway.processing.store import ProcessingStore

from tests.gateway.history_test_helpers import ProjectedHistory

CHAT = "120000000000000042@g.us"
MIN = 60_000
T0 = 1_790_000_000_000  # synthetic
SECRET = "synthetic-private-wording"
SUBJECT = "Synthetic Test Group"


def _history(tmp_path: Path, *, subject: str | None = None) -> ProjectedHistory:
    history = ProjectedHistory(tmp_path / "history")
    if subject is not None:
        from tests.gateway.convhist.hist_fixtures import _raw

        history.records.append(_raw("group_subject", "group_subject", dict(
            chatJid=CHAT, value=subject, snapshot=True, observedAtMs=T0 - MIN), received=T0 - MIN))

    def say(message_id: str, minute: int, sender: str, *, out: bool = False) -> None:
        history.add_message(
            chat_id=CHAT, message_id=message_id, text=f"{SECRET} {message_id}",
            timestamp=(T0 + minute * MIN) // 1000, sender_id=None if out else sender,
            participant=None if out else sender, direction="out" if out else "in",
        )

    say("old-1", 0, "10001@s.whatsapp.net")
    say("old-2", 2, "10002@s.whatsapp.net")
    say("new-1", 60, "10003@s.whatsapp.net")
    say("arvid-1", 61, "", out=True)
    say("new-2", 62, "10001@s.whatsapp.net")
    say("later", 70, "10002@s.whatsapp.net")  # after the opportunity: excluded
    history.append("reaction", chat_id=CHAT, timestamp=(T0 + 62 * MIN) // 1000, emoji="🔥",
                   removed=False, senderId="10002@s.whatsapp.net", targetMessageId="new-1")
    history.append("reaction", chat_id=CHAT, timestamp=(T0 + 69 * MIN) // 1000, emoji="😂",
                   removed=False, senderId="10003@s.whatsapp.net", targetMessageId="new-1")
    history.snapshot().close()  # project once; the replay opens its own snapshots
    return history


def _speakups(path: Path) -> Path:
    SpeakupLog(path).close()
    with sqlite3.connect(path) as db:
        for opportunity, minute, sources, outcome in (
            ("opp-1", 63, ["new-2"], "silence"),
            ("opp-2", 3, ["old-2"], "react"),
            ("opp-3", 64, [], "provider_error"),
        ):
            db.execute(
                "INSERT INTO judge_attempts (attempt_id, opportunity_id, evaluation_index, channel,"
                " chat_id, created_at_ms, hourly_limit, outcome) VALUES (?,?,0,'whatsapp',?,?,12,?)",
                (f"{opportunity}:0", opportunity, CHAT, T0 + minute * MIN, outcome),
            )
            db.execute(
                "INSERT INTO opportunity_dispositions (opportunity_id, channel, chat_id,"
                " observed_revision, activation_epoch, lane, trigger, source_ids_json,"
                " disposition, created_at_ms, updated_at_ms)"
                " VALUES (?, 'whatsapp', ?, 1, 1, 'production', 'inbound', ?, 'judged', ?, ?)",
                (opportunity, CHAT, json.dumps(sources), T0, T0),
            )
        # A re-evaluation of the same opportunity is not a second opportunity.
        db.execute(
            "INSERT INTO judge_attempts (attempt_id, opportunity_id, evaluation_index, channel,"
            " chat_id, created_at_ms, hourly_limit, outcome)"
            " VALUES ('opp-1:1','opp-1',1,'whatsapp',?,?,12,'silence')",
            (CHAT, T0 + 64 * MIN),
        )
    return path


class _FakeJudgeClient:
    """Reacts on the window view and stays silent on the conversation view."""

    route_key = "eval.fake"
    structured_output = "json_schema_strict"
    model = "fake"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def chat_with_usage(self, messages, *, max_tokens, response_format=None,
                              max_retries=None) -> RouteReply:
        prompt = messages[-1]["content"]
        self.prompts.append(prompt)
        conversation = "Conversation since" in prompt
        content = (
            '{"action":"silence","intent":"initiate","reason":"x"}' if conversation
            else '{"action":"react","intent":"initiate","emoji":"👍","reason":"x"}'
        )
        return RouteReply(content=content, usage={"prompt_tokens": len(prompt) // 4},
                          model="fake", finish_reason="stop")


def _bounds(mode: str, *, floor: int = 0) -> ParticipationContextBounds:
    return ParticipationContextBounds(
        window_minutes=120, max_messages=40, context_mode=mode,
        lull_minutes=25, burst_messages=4, burst_minutes=15, conversation_min_messages=floor,
    )


class _SlowFakeJudgeClient(_FakeJudgeClient):
    """The fake Judge with overlapping calls that finish out of order."""

    def __init__(self) -> None:
        super().__init__()
        self.in_flight = 0
        self.max_in_flight = 0

    async def chat_with_usage(self, messages, **kwargs) -> RouteReply:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            # Earlier calls wait longer, so completion order differs from start order.
            await asyncio.sleep(0.05 / (1 + len(self.prompts)))
            return await super().chat_with_usage(messages, **kwargs)
        finally:
            self.in_flight -= 1


async def _run(tmp_path: Path, *, judged: bool, registry=None, client=None, concurrency: int = 1,
               subject: str | None = None, judge_repeats: int = 1, floor: int = 0):
    history = _history(tmp_path, subject=subject)
    opportunities = load_opportunities(
        _speakups(tmp_path / "speakups.db"), since_ms=T0, until_ms=T0 + 100 * MIN
    )
    capture = client is not None
    client = client or _FakeJudgeClient()
    last: dict[str, object] = {}

    class _Recording(_FakeJudgeClient):
        async def chat_with_usage(self, messages, **kwargs) -> RouteReply:
            reply = await client.chat_with_usage(messages, **kwargs)
            last["usage"] = reply.usage
            return reply

    as_of = {"ms": 0}
    builder = ParticipationContextBuilder(
        policy=None, source_authorizer=lambda row: True,
        thread_lookup=(lambda native: registry(native, as_of["ms"])) if registry else None,
    )
    frozen = FrozenHistory(history.path)
    try:
        records = await replay(
            opportunities, history=frozen, builder=builder,
            window_bounds=_bounds("window"),
            conversation_bounds=_bounds("conversation", floor=floor),
            estimator=ParticipationJudge(client=StaticClient("{}"), allowed_emojis=("👍",)),
            judge=ParticipationJudge(
                client=UsageCapture(client) if capture else _Recording(), allowed_emojis=("👍",),
            ) if judged else None,
            usage=captured_usage if capture else (lambda: last.get("usage") or {}),
            set_as_of=lambda moment: as_of.__setitem__("ms", moment),
            concurrency=concurrency, judge_repeats=judge_repeats,
        )
    finally:
        frozen.close()
        history.close()
    return opportunities, records, client


@pytest.mark.asyncio
async def test_replay_rebuilds_both_views_as_of_the_opportunity(tmp_path: Path) -> None:
    opportunities, records, _ = await _run(tmp_path, judged=False)
    assert [item.opportunity_id for item in opportunities] == ["opp-2", "opp-1", "opp-3"]
    by_id = {record["opportunity_id"]: record for record in records}
    first = by_id["opp-1"]
    assert first["A"]["message_ids"] == ["old-1", "old-2", "new-1", "new-2"]
    assert first["B"]["message_ids"] == ["new-1", "new-2"]
    assert first["B"]["context_mode"] == "conversation"
    stats = first["B"]["conversation"]
    assert stats["reaction_count"] == 1  # the 😂 came after the opportunity
    assert stats["message_count"] == 3 and stats["arvid_messages"] == 1
    assert first["B"]["estimated_prompt_tokens"] < first["A"]["estimated_prompt_tokens"]
    # The older opportunity only sees history up to its own time.
    assert by_id["opp-2"]["A"]["message_ids"] == ["old-1", "old-2"]
    assert by_id["opp-3"]["skipped"] == "no_history_sources"
    summary = summarize(records, judged=False)
    assert summary["built_both"] == 2
    deltas = [by_id[key]["B"]["estimated_prompt_tokens"] - by_id[key]["A"]["estimated_prompt_tokens"]
              for key in ("opp-1", "opp-2")]
    assert summary["estimated_token_delta_mean"] == sum(deltas) / 2
    assert deltas[1] > 0  # same messages plus the one header line
    assert "agreement_rate" not in summary


@pytest.mark.asyncio
async def test_replay_with_a_fake_judge_reports_agreement_and_disagreements(
    tmp_path: Path,
) -> None:
    _, records, client = await _run(tmp_path, judged=True)
    by_id = {record["opportunity_id"]: record for record in records}
    assert by_id["opp-1"]["A"]["action"] == "react"
    assert by_id["opp-1"]["B"]["action"] == "silence"
    assert isinstance(by_id["opp-1"]["B"]["prompt_tokens"], int)
    assert all("later" not in prompt and "😂" not in prompt.split("\n")[0]
               for prompt in client.prompts)
    summary = summarize(records, judged=True)
    assert summary["decided_both"] == 2
    assert summary["agreement_rate"] == 0.0
    assert summary["disagreement_ids"] == ["opp-2", "opp-1"]
    assert by_id["opp-1"]["B"]["prompt_tokens"] < by_id["opp-1"]["A"]["prompt_tokens"]
    assert isinstance(summary["prompt_token_delta_mean"], float)

    directory = tmp_path / "report"
    directory.mkdir()
    write_outputs(directory, records, summary, "# report\n")
    written = "".join(path.read_text() for path in directory.iterdir())
    assert SECRET not in written
    assert "reason" not in (directory / "records.jsonl").read_text()
    lines = (directory / "records.jsonl").read_text().splitlines()
    assert len(lines) == 3 and json.loads(lines[0])["opportunity_id"] == "opp-2"


def test_disagreement_sample_is_capped_at_thirty() -> None:
    records = [
        {"opportunity_id": f"o{index:02d}",
         "A": {"status": "decided", "action": "react", "messages": 2},
         "B": {"status": "decided", "action": "silence", "messages": 1}}
        for index in range(45)
    ]
    summary = summarize(records, judged=True)
    assert summary["disagreement_count"] == 45
    assert len(summary["disagreement_ids"]) == 30
    assert summary["disagreement_ids"] == sorted(summary["disagreement_ids"])


@pytest.mark.asyncio
async def test_registry_lookup_is_as_of_the_opportunity(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "processing.db")
    try:
        store.register_thread_message(
            thread_id="thread-1", now_ms=T0 + 61 * MIN, effect_id="effect-1",
            message_id="arvid-1",
        )
    finally:
        store.close()
    lookup = registry_lookup(tmp_path / "processing.db")
    assert lookup is not None
    assert lookup("arvid-1", T0 + 62 * MIN) is not None
    assert lookup("arvid-1", T0 + 60 * MIN) is None
    _, records, _ = await _run(tmp_path, judged=False, registry=lookup)
    by_id = {record["opportunity_id"]: record for record in records}
    assert by_id["opp-1"]["B"]["conversation"]["arvid_addressed"] is True


def test_cli_no_judge_writes_jsonl_and_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import yeoman_gateway.evaluation.participation_context_ab as ab

    home = tmp_path / "home"
    monkeypatch.setenv("YEOMAN_HOME", str(home))
    history = _history(tmp_path)
    (home / "data" / "history").mkdir(parents=True)
    shutil.copy(history.path, home / "data" / "history" / "history.db")
    history.close()
    (home / "data" / "ops").mkdir(parents=True)
    _speakups(home / "data" / "ops" / "speakups.db")
    original = ab.load_opportunities
    monkeypatch.setattr(
        ab, "load_opportunities",
        lambda path, *, since_ms, until_ms, chat_ids=None: original(
            path, since_ms=T0, until_ms=T0 + 100 * MIN, chat_ids=chat_ids),
    )
    result = CliRunner().invoke(app, ["eval", "participation-context", "--days", "14", "--no-judge"])
    assert result.exit_code == 0, result.output
    [directory] = list((home / "var" / "eval" / "participation-context-ab").glob("*-replay"))
    summary = json.loads((directory / "summary.json").read_text())
    assert summary["opportunities"] == 3
    assert summary["judged"] is False
    assert len((directory / "records.jsonl").read_text().splitlines()) == 3
    assert SECRET not in (directory / "records.jsonl").read_text()
    assert (directory / "summary.md").read_text().startswith("# Participation context A/B")


@pytest.mark.asyncio
async def test_parallel_judge_calls_are_bounded_and_match_a_sequential_run(
    tmp_path: Path,
) -> None:
    sequential_client = _SlowFakeJudgeClient()
    _, sequential, _ = await _run(tmp_path / "seq", judged=True, client=sequential_client)
    parallel_client = _SlowFakeJudgeClient()
    _, parallel, _ = await _run(tmp_path / "par", judged=True, client=parallel_client,
                                concurrency=3)
    assert sequential_client.max_in_flight == 1
    assert parallel_client.max_in_flight == 3  # four Judge calls, three slots
    assert len(parallel_client.prompts) == 4
    assert parallel == sequential
    assert [record["opportunity_id"] for record in parallel] == ["opp-2", "opp-1", "opp-3"]
    assert summarize(parallel, judged=True) == summarize(sequential, judged=True)
    # Usage is attributed per call, not taken from whichever call finished last.
    by_id = {record["opportunity_id"]: record for record in parallel}
    assert by_id["opp-1"]["B"]["prompt_tokens"] < by_id["opp-1"]["A"]["prompt_tokens"]


def test_load_opportunities_filters_by_chat(tmp_path: Path) -> None:
    speakups = _speakups(tmp_path / "speakups.db")
    kept = load_opportunities(speakups, since_ms=T0, until_ms=T0 + 100 * MIN, chat_ids=[CHAT])
    assert len(kept) == 3
    assert load_opportunities(speakups, since_ms=T0, until_ms=T0 + 100 * MIN,
                              chat_ids=["120000000000000099@g.us"]) == []


def test_resolve_chat_by_id_or_exact_subject() -> None:
    subjects = {CHAT: SUBJECT, "1@g.us": "Other Private Group", "2@g.us": "Twin",
                "3@g.us": "twin"}
    assert resolve_chat("999@g.us", subjects) == "999@g.us"  # ids pass through
    assert resolve_chat("-100123", subjects) == "-100123"
    assert resolve_chat("whatsapp:999@g.us", subjects) == "999@g.us"  # config-style key
    assert resolve_chat("telegram:-100123", subjects) == "-100123"
    assert resolve_chat("synthetic test GROUP", subjects) == CHAT
    with pytest.raises(ValueError, match="0 matches") as none:
        resolve_chat("Synthetic", subjects)  # exact match only
    with pytest.raises(ValueError, match="2 matches") as ambiguous:
        resolve_chat("TWIN", subjects)
    for error in (none, ambiguous):
        assert "Other Private" not in str(error.value) and "Twin" not in str(error.value)


@pytest.mark.asyncio
async def test_chat_subjects_from_history(tmp_path: Path) -> None:
    history = _history(tmp_path, subject=SUBJECT)
    frozen = FrozenHistory(history.path)
    try:
        assert await chat_subjects(frozen) == {CHAT: SUBJECT}
    finally:
        frozen.close()
        history.close()


def _cli_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import yeoman_gateway.evaluation.participation_context_ab as ab

    home = tmp_path / "home"
    monkeypatch.setenv("YEOMAN_HOME", str(home))
    history = _history(tmp_path, subject=SUBJECT)
    (home / "data" / "history").mkdir(parents=True)
    shutil.copy(history.path, home / "data" / "history" / "history.db")
    history.close()
    (home / "data" / "ops").mkdir(parents=True)
    _speakups(home / "data" / "ops" / "speakups.db")
    original = ab.load_opportunities
    monkeypatch.setattr(
        ab, "load_opportunities",
        lambda path, *, since_ms, until_ms, chat_ids=None: original(
            path, since_ms=T0, until_ms=T0 + 100 * MIN, chat_ids=chat_ids),
    )
    return home


def _replay_summary(home: Path, *args: str) -> dict:
    result = CliRunner().invoke(app, ["eval", "participation-context", "--no-judge", *args])
    assert result.exit_code == 0, result.output
    directory = sorted((home / "var" / "eval" / "participation-context-ab").glob("*-replay"))[-1]
    summary = json.loads((directory / "summary.json").read_text())
    shutil.rmtree(directory)
    return summary


def test_cli_chat_filter_by_id_and_subject(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _cli_home(tmp_path, monkeypatch)
    assert _replay_summary(home, "--chat", CHAT)["opportunities"] == 3
    assert _replay_summary(home, "--chat", "120000000000000099@g.us")["opportunities"] == 0
    assert _replay_summary(home, "--chat", SUBJECT.upper())["opportunities"] == 3
    result = CliRunner().invoke(
        app, ["eval", "participation-context", "--no-judge", "--chat", "No Such Group"]
    )
    assert result.exit_code == 2
    assert "0 matches" in result.output and SUBJECT not in result.output


def test_cli_review_labels_disagreements_without_writing_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _cli_home(tmp_path, monkeypatch)
    _, records, _ = asyncio.run(_run(tmp_path / "judged", judged=True))
    summary = summarize(records, judged=True)
    report = home / "var" / "eval" / "participation-context-ab" / "20261012-replay"
    report.mkdir(parents=True)
    write_outputs(report, records, summary, "# report\n")
    command = ["eval", "participation-context-review", str(report)]

    first = CliRunner().invoke(app, command, input="x\nb\nq\n")
    assert first.exit_code == 0, first.output
    assert "=== 1/2 opp-2" in first.output and "=== 2/2 opp-1" in first.output  # then quit
    assert f"chat: {SUBJECT}" in first.output
    assert SECRET in first.output  # the owner sees the messages in the terminal
    assert "Conversation since" in first.output  # B header line
    assert "A decision: react 👍" in first.output and "B decision: silence" in first.output
    assert "please answer" in first.output
    assert "labelled=1 a_better=0 b_better=1 same=0 b_better_or_same=1.0" in first.output

    second = CliRunner().invoke(app, command, input="a\n")
    assert second.exit_code == 0, second.output
    assert "=== 1/1 opp-1" in second.output and "opp-2" not in second.output
    assert "b_better_or_same=0.5 (threshold 70%: not met)" in second.output

    labels = [json.loads(line) for line in (report / "labels.jsonl").read_text().splitlines()]
    assert [(item["opportunity_id"], item["label"]) for item in labels] == [
        ("opp-2", "b_better"), ("opp-1", "a_better"),
    ]
    assert all(set(item) == {"opportunity_id", "label", "labelled_at"} for item in labels)
    for path in report.iterdir():
        assert SECRET not in path.read_text()

    third = CliRunner().invoke(app, command, input="")
    assert "nothing to label" in third.output
    only = CliRunner().invoke(app, [*command, "--summary"])
    assert only.exit_code == 0 and SECRET not in only.output
    assert "labelled=2 a_better=1 b_better=1 same=0" in only.output


def test_label_summary_threshold() -> None:
    stats = label_summary({"o1": "b_better", "o2": "same", "o3": "b_better", "o4": "a_better"})
    assert stats["b_better_or_same_share"] == 0.75 and stats["meets_threshold"] is True
    assert label_summary({})["b_better_or_same_share"] is None


class _CyclingJudgeClient(_FakeJudgeClient):
    """Window view: always react. Conversation view: the next action of a fixed cycle."""

    def __init__(self, cycle: tuple[str, ...]) -> None:
        super().__init__()
        self.cycle = cycle
        self.conversation_calls = 0

    async def chat_with_usage(self, messages, **kwargs) -> RouteReply:
        prompt = messages[-1]["content"]
        if "Conversation since" not in prompt:
            return await super().chat_with_usage(messages, **kwargs)
        self.prompts.append(prompt)
        action = self.cycle[self.conversation_calls % len(self.cycle)]
        self.conversation_calls += 1
        if action == "error":
            content = "not json"
        elif action == "react":
            content = '{"action":"react","intent":"initiate","emoji":"👍","reason":"x"}'
        else:
            content = '{"action":"silence","intent":"initiate","reason":"x"}'
        return RouteReply(content=content, usage={"prompt_tokens": len(prompt) // 4},
                          model="fake", finish_reason="stop")


@pytest.mark.asyncio
async def test_floor_in_the_replay_adds_earlier_context_to_b(tmp_path: Path) -> None:
    _, records, _ = await _run(tmp_path, judged=False, floor=8)
    by_id = {record["opportunity_id"]: record for record in records}
    b = by_id["opp-1"]["B"]
    assert b["message_ids"] == ["old-1", "old-2", "new-1", "new-2"]
    assert b["conversation"]["earlier_messages"] == 2
    assert b["conversation"]["message_count"] == 3  # the conversation itself only


@pytest.mark.asyncio
async def test_judge_repeats_record_the_distribution_and_use_the_majority(
    tmp_path: Path,
) -> None:
    client = _CyclingJudgeClient(("silence", "react", "silence"))
    _, records, _ = await _run(tmp_path, judged=True, client=client, judge_repeats=3)
    by_id = {record["opportunity_id"]: record for record in records}
    assert client.conversation_calls == 6  # two opportunities, three B repeats each
    for opportunity_id in ("opp-1", "opp-2"):
        b = by_id[opportunity_id]["B"]
        assert b["status"] == "decided"
        assert b["judge_repeats"] == 3
        assert sorted(b["action_distribution"].items()) == [("react", 1), ("silence", 2)]
        assert b["action"] == "silence" and b["unanimous"] is False
        assert [run["action"] for run in b["repeats"]] == ["silence", "react", "silence"]
        assert "repeats" not in by_id[opportunity_id]["A"]  # A is asked once
    summary = summarize(records, judged=True)
    assert summary["judge_repeats"] == 3
    assert summary["b_unanimous_share"] == 0.0
    assert summary["b_split_ids"] == ["opp-2", "opp-1"]
    assert summary["actions"]["B"] == {"silence": 2}
    assert "reason" not in json.dumps(records)


@pytest.mark.asyncio
async def test_judge_repeats_ties_take_the_earliest_and_errors_do_not_vote(
    tmp_path: Path,
) -> None:
    client = _CyclingJudgeClient(("react", "error", "silence"))
    _, records, _ = await _run(tmp_path / "tie", judged=True, client=client, judge_repeats=3)
    b = {record["opportunity_id"]: record for record in records}["opp-2"]["B"]
    assert b["action"] == "react" and b["emoji"] == "👍"
    assert b["action_distribution"] == {"react": 1, "silence": 1}
    assert [run["status"] for run in b["repeats"]] == ["decided", "judge_error", "decided"]
    assert b["unanimous"] is False and "error" not in b

    failing = _CyclingJudgeClient(("error",))
    _, records, _ = await _run(tmp_path / "err", judged=True, client=failing, judge_repeats=2)
    b = {record["opportunity_id"]: record for record in records}["opp-1"]["B"]
    assert b["status"] == "judge_error" and b["action_distribution"] == {}
    assert summarize(records, judged=True)["decided_both"] == 0


@pytest.mark.asyncio
async def test_unanimous_repeats(tmp_path: Path) -> None:
    client = _CyclingJudgeClient(("silence",))
    _, records, _ = await _run(tmp_path, judged=True, client=client, judge_repeats=2)
    summary = summarize(records, judged=True)
    assert summary["b_unanimous_share"] == 1.0 and summary["b_split_ids"] == []


def _decided(action: str) -> dict[str, object]:
    return {"status": "decided", "action": action}


def test_label_check_scores_new_b_against_source_labels() -> None:
    source = [
        {"opportunity_id": "a1", "A": _decided("react"), "B": _decided("silence")},
        {"opportunity_id": "a2", "A": _decided("react"), "B": _decided("silence")},
        {"opportunity_id": "b1", "A": _decided("react"), "B": _decided("silence")},
        {"opportunity_id": "b2", "A": _decided("silence"), "B": _decided("comment")},
        {"opportunity_id": "s1", "A": _decided("react"), "B": _decided("silence")},
        {"opportunity_id": "s2", "A": _decided("react"), "B": _decided("silence")},
        {"opportunity_id": "u1", "A": _decided("react"), "B": {"status": "judge_error"}},
    ]
    new = [
        {"opportunity_id": "a1", "B": _decided("react")},     # now matches A -> same
        {"opportunity_id": "a2", "B": _decided("silence")},   # still B -> a_better
        {"opportunity_id": "b1", "B": _decided("silence")},   # still source B -> b_better
        {"opportunity_id": "b2", "B": _decided("react")},     # changed -> unknown
        {"opportunity_id": "s1", "B": _decided("react")},     # equals A -> same
        {"opportunity_id": "s2", "B": _decided("comment")},   # neither -> unknown
        {"opportunity_id": "u1", "B": _decided("react")},     # source B undecided
    ]
    labels = {"a1": "a_better", "a2": "a_better", "b1": "b_better", "b2": "b_better",
              "s1": "same", "s2": "same", "u1": "a_better", "gone": "same"}
    check = label_check(new, source, labels)
    assert check["labelled"] == 8 and check["checked"] == 6
    assert check["unchecked_ids"] == ["gone", "u1"]
    assert check["by_label"]["a_better"] == {"labelled": 3, "checked": 2, "matches": 1, "share": 0.5}
    assert check["by_label"]["b_better"] == {"labelled": 2, "checked": 2, "matches": 1, "share": 0.5}
    assert check["by_label"]["same"] == {"labelled": 3, "checked": 2, "matches": 1, "share": 0.5}
    assert check["estimated"] == {"a_better": 1, "b_better": 1, "same": 2, "unknown": 2}
    assert check["estimated_b_better_or_same_share"] == 0.5
    assert check["meets_threshold"] is False
    empty = label_check([], [], {})
    assert empty["checked"] == 0 and empty["estimated_b_better_or_same_share"] is None


def test_cli_from_report_replays_the_same_ids_and_checks_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import yeoman_gateway.evaluation.judge_format as judge_format
    import yeoman_gateway.evaluation.participation_context_ab as ab

    home = _cli_home(tmp_path, monkeypatch)
    _, records, _ = asyncio.run(_run(tmp_path / "judged", judged=True))
    source = tmp_path / "source-report"
    source.mkdir()
    write_outputs(source, records, summarize(records, judged=True), "# report\n")
    # Source: A reacted and B stayed silent on both opportunities.
    (source / "labels.jsonl").write_text(
        json.dumps({"opportunity_id": "opp-1", "label": "a_better", "labelled_at": "x"}) + "\n"
        + json.dumps({"opportunity_id": "opp-2", "label": "b_better", "labelled_at": "x"}) + "\n",
        encoding="utf-8",
    )

    def no_speakups(*args, **kwargs):
        raise AssertionError("--from-report must not read speakups.db")

    monkeypatch.setattr(ab, "load_opportunities", no_speakups)
    client = _CyclingJudgeClient(("react",))  # the new B now reacts everywhere
    monkeypatch.setattr(judge_format, "build_eval_client", lambda config, **kwargs: client)
    result = CliRunner().invoke(app, [
        "eval", "participation-context", "--from-report", str(source),
        "--days", "1", "--limit", "1", "--judge-repeats", "2", "--concurrency", "1",
    ])
    assert result.exit_code == 0, result.output
    assert "model calls: up to 9" in result.output
    assert "label_check=0.5 of 2" in result.output
    [directory] = list((home / "var" / "eval" / "participation-context-ab").glob("*-replay"))
    summary = json.loads((directory / "summary.json").read_text())
    replayed = [json.loads(line)["opportunity_id"]
                for line in (directory / "records.jsonl").read_text().splitlines()]
    assert replayed == ["opp-2", "opp-1", "opp-3"]  # --days/--limit do not apply
    assert summary["source_report"] == "source-report"
    assert summary["judge_repeats"] == 2
    check = summary["label_check"]
    assert check["by_label"]["a_better"]["matches"] == 1  # new B == source A -> same
    assert check["by_label"]["b_better"]["matches"] == 0  # new B left source B -> unknown
    assert check["estimated"] == {"a_better": 0, "b_better": 0, "same": 1, "unknown": 1}
    markdown = (directory / "summary.md").read_text()
    assert "exactly those of report `source-report`" in markdown
    assert "## Label check" in markdown and "unknown counts against B" in markdown
    for path in directory.iterdir():
        assert SECRET not in path.read_text()

    filtered = CliRunner().invoke(app, [
        "eval", "participation-context", "--from-report", str(source), "--no-judge",
        "--chat", "120000000000000099@g.us",
    ])
    assert filtered.exit_code == 0, filtered.output
    assert "opportunities=0" in filtered.output and "label_check" not in filtered.output

    missing = CliRunner().invoke(app, [
        "eval", "participation-context", "--from-report", str(tmp_path / "nope"), "--no-judge",
    ])
    assert missing.exit_code == 2 and "records.jsonl missing" in missing.output
