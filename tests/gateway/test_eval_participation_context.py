"""Offline A/B replay of the participation context (Spec A section 7 step 1).

Synthetic stores and a fake Judge only. Nothing here talks to a provider.
"""

from __future__ import annotations

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
    load_opportunities,
    registry_lookup,
    replay,
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


def _history(tmp_path: Path) -> ProjectedHistory:
    history = ProjectedHistory(tmp_path / "history")

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


def _bounds(mode: str) -> ParticipationContextBounds:
    return ParticipationContextBounds(
        window_minutes=120, max_messages=40, context_mode=mode,
        lull_minutes=25, burst_messages=4, burst_minutes=15,
    )


async def _run(tmp_path: Path, *, judged: bool, registry=None):
    history = _history(tmp_path)
    opportunities = load_opportunities(
        _speakups(tmp_path / "speakups.db"), since_ms=T0, until_ms=T0 + 100 * MIN
    )
    client = _FakeJudgeClient()
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
            window_bounds=_bounds("window"), conversation_bounds=_bounds("conversation"),
            estimator=ParticipationJudge(client=StaticClient("{}"), allowed_emojis=("👍",)),
            judge=ParticipationJudge(client=_Recording(), allowed_emojis=("👍",)) if judged else None,
            usage=lambda: last.get("usage") or {},
            set_as_of=lambda moment: as_of.__setitem__("ms", moment),
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
        lambda path, *, since_ms, until_ms: original(path, since_ms=T0, until_ms=T0 + 100 * MIN),
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
