"""Offline evaluation commands for synthetic Participation and retrieval scenarios."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path

import typer

from .core import app

eval_app = typer.Typer(help="Offline evaluation of Participation decisions and memory retrieval")
app.add_typer(eval_app, name="eval")

SILENCE_STUB = '{"action": "silence", "intent": "initiate", "reason": "stub"}'


@eval_app.command("retrieval")
def eval_retrieval() -> None:
    """Score today's bounded Participation view on the synthetic retrieval set."""
    from yeoman_gateway.evaluation.report import new_report_dir, write_report
    from yeoman_gateway.evaluation.retrieval import (
        generate_retrieval_set,
        score_retrieval,
        today_participation_view,
    )

    summary = asyncio.run(score_retrieval(generate_retrieval_set(), today_participation_view))
    lines = [
        "# Retrieval evaluation (today's Participation view)",
        "",
        "| category | needed recall | full recall | abstention | tokens | build p95 ms |",
        "|---|---|---|---|---|---|",
    ]
    for category, metrics in summary.by_category.items():
        lines.append(
            f"| {category} | {metrics['needed_recall']} | {metrics['full_recall_rate']} | "
            f"{metrics['abstention_correct']} | {metrics['tokens_mean']} | "
            f"{metrics['build_ms_p95']} |"
        )
    directory = new_report_dir("retrieval")
    write_report(directory, asdict(summary), "\n".join(lines) + "\n")
    typer.echo(f"report: {directory}")


@eval_app.command("discretion")
def eval_discretion(
    route: str = typer.Option("participation.judge", "--route"),
    profile: str | None = typer.Option(None, "--profile"),
    structured_output: str | None = typer.Option(None, "--structured-output"),
    runs: int = typer.Option(3, "--runs", min=1, max=50),
    stub: bool = typer.Option(False, "--stub", help="Always-silent stub; no model calls"),
) -> None:
    """Run the synthetic discretion suite against today's Judge view."""
    from yeoman_gateway.evaluation.discretion import DISCRETION_FILE, run_discretion
    from yeoman_gateway.evaluation.harness import StaticClient, make_judge
    from yeoman_gateway.evaluation.report import new_report_dir, write_report
    from yeoman_gateway.evaluation.scenarios import FEATURES_AVAILABLE, load_scenario_file

    scenarios = load_scenario_file(DISCRETION_FILE)
    runnable = [scenario for scenario in scenarios if set(scenario.requires) <= FEATURES_AVAILABLE]
    if stub:
        client = StaticClient(SILENCE_STUB)
        emojis = ("👍",)
    else:
        from yeoman_shared.config.loader import load_config

        from yeoman_gateway.evaluation.judge_format import build_eval_client
        from yeoman_gateway.processing.model_route import RouteUnavailableError

        config = load_config()
        try:
            client = build_eval_client(
                config,
                route=None if profile else route,
                profile=profile,
                structured_output=structured_output,
            )
        except RouteUnavailableError as exc:
            typer.echo(f"error: {exc}")
            raise typer.Exit(2) from exc
        emojis = tuple(config.processing.reaction_emojis)
        typer.echo(f"model calls: {len(runnable) * runs} ({len(runnable)} scenarios x {runs} runs)")

    results = asyncio.run(
        run_discretion(
            scenarios,
            lambda: make_judge(client, allowed_emojis=emojis),
            runs=runs,
        )
    )
    client_info = (
        f"client: {getattr(client, 'route_key', '')} model={getattr(client, 'model', '')} "
        f"mode={getattr(client, 'structured_output', '')}"
    )
    lines = [
        "# Discretion suite",
        "",
        client_info,
        "",
        "| scenario | status | actions | detail |",
        "|---|---|---|---|",
    ]
    lines.extend(
        f"| {result.scenario_id} | {result.status} | {','.join(result.actions)} | {result.detail} |"
        for result in results
    )
    directory = new_report_dir("discretion")
    write_report(directory, {"results": [asdict(result) for result in results]}, "\n".join(lines) + "\n")
    typer.echo(f"report: {directory}")


@eval_app.command("judge-format")
def eval_judge_format(
    route: str | None = typer.Option(None, "--route"),
    profile: str | None = typer.Option(None, "--profile"),
    structured_output: str | None = typer.Option(None, "--structured-output"),
    runs: int = typer.Option(20, "--runs", min=1, max=200),
    record: bool = typer.Option(False, "--record", help="Keep raw synthetic outputs"),
) -> None:
    """Measure Judge parse failures and latency for a route or profile."""
    from yeoman_shared.config.loader import load_config

    from yeoman_gateway.evaluation.discretion import DISCRETION_FILE
    from yeoman_gateway.evaluation.judge_format import build_eval_client, run_judge_format
    from yeoman_gateway.evaluation.report import new_report_dir, write_report
    from yeoman_gateway.evaluation.scenarios import load_scenario_file
    from yeoman_gateway.processing.model_route import RouteUnavailableError

    config = load_config()
    try:
        client = build_eval_client(
            config,
            route=route,
            profile=profile,
            structured_output=structured_output,
        )
    except RouteUnavailableError as exc:
        typer.echo(f"error: {exc}")
        raise typer.Exit(2) from exc

    scenarios = [scenario for scenario in load_scenario_file(DISCRETION_FILE) if not scenario.requires]
    typer.echo(f"model calls: {len(scenarios) * runs}")
    directory = new_report_dir("judge-format")
    summary = asyncio.run(
        run_judge_format(
            client,
            scenarios,
            runs=runs,
            allowed_emojis=tuple(config.processing.reaction_emojis),
            record_dir=directory / "raw" if record else None,
        )
    )
    markdown = (
        f"# Judge output format\n\n{asdict(summary)}\n\n"
        "Gate (V1 §4.4): failure rate ≤ 0.01 over ≥ 300 attempts.\n"
    )
    write_report(directory, asdict(summary), markdown)
    typer.echo(
        f"failure_rate={summary.failure_rate} p50={summary.latency_ms_p50}ms "
        f"report: {directory}"
    )


def _participation_replay_setup(config):
    """Builder, both bounds, estimator and as-of hooks shared by replay and review."""
    from types import SimpleNamespace

    from yeoman_gateway.app.bootstrap import _processing_store_path, participant_is_allowed
    from yeoman_gateway.evaluation.harness import StaticClient
    from yeoman_gateway.evaluation.participation_context_ab import registry_lookup
    from yeoman_gateway.processing.participation import ParticipationJudge
    from yeoman_gateway.processing.participation_context import (
        ParticipationContextBounds,
        ParticipationContextBuilder,
    )

    from .core import make_policy_engine

    participation = config.processing.participation
    engine, _policy_path = make_policy_engine(config)
    emojis = tuple(config.processing.reaction_emojis)
    max_input = int(participation.judge_max_input_tokens)
    lookup = registry_lookup(_processing_store_path(config))
    as_of = {"ms": 0}
    builder = ParticipationContextBuilder(
        policy=engine,
        source_authorizer=lambda row: participant_is_allowed(
            engine=engine, channel=str(row.get("channel") or ""),
            chat_id=str(row.get("chat_id") or ""),
            sender=str(row.get("sender_id") or row.get("participant") or ""),
        ),
        thread_lookup=(lambda native: lookup(native, as_of["ms"])) if lookup else None,
    )
    shared = {
        "window_minutes": int(participation.context_window_minutes),
        "max_messages": int(participation.context_max_messages),
        "lull_minutes": int(config.consciousness.lull_silence_minutes),
        "burst_messages": int(config.consciousness.burst_threshold_messages),
        "burst_minutes": int(config.consciousness.burst_window_minutes),
    }

    def guidance_for(channel: str, chat_id: str) -> str:
        return str(getattr(engine.resolve_participation(channel, chat_id), "guidance", "") or "")

    return SimpleNamespace(
        emojis=emojis, max_input=max_input,
        replay_args={
            "builder": builder,
            "window_bounds": ParticipationContextBounds(context_mode="window", **shared),
            "conversation_bounds": ParticipationContextBounds(context_mode="conversation", **shared),
            "estimator": ParticipationJudge(
                client=StaticClient("{}"), allowed_emojis=emojis, max_input_tokens=max_input
            ),
            "guidance_for": guidance_for,
            "set_as_of": lambda moment: as_of.__setitem__("ms", moment),
        },
    )


@eval_app.command("participation-context")
def eval_participation_context(
    days: int = typer.Option(14, "--days", min=1, max=90),
    no_judge: bool = typer.Option(
        False, "--no-judge", help="Context sizes and token estimates only; no model calls"
    ),
    route: str = typer.Option("participation.judge", "--route"),
    profile: str | None = typer.Option(None, "--profile"),
    limit: int | None = typer.Option(None, "--limit", min=1, help="Newest N opportunities only"),
    concurrency: int = typer.Option(6, "--concurrency", min=1, help="Parallel Judge calls"),
    chats: list[str] | None = typer.Option(
        None, "--chat", help="Only this chat (id or exact group subject); repeatable"
    ),
) -> None:
    """Replay recorded Judge opportunities with the window (A) and conversation (B) views."""
    import time

    from yeoman_shared.config.loader import load_config
    from yeoman_shared.utils.helpers import get_operational_store_path, get_var_path

    from yeoman_gateway.evaluation.participation_context_ab import (
        REPORT_DIR,
        FrozenHistory,
        UsageCapture,
        captured_usage,
        chat_subjects,
        load_opportunities,
        looks_like_chat_id,
        render_markdown,
        replay,
        resolve_chat,
        summarize,
        write_outputs,
    )
    from yeoman_gateway.evaluation.report import new_report_dir
    from yeoman_gateway.history.live import history_db_path
    from yeoman_gateway.processing.participation import ParticipationJudge

    config = load_config()
    participation = config.processing.participation
    speakups = get_operational_store_path("speakups", create=False)
    if not speakups.is_file() or not history_db_path().is_file():
        typer.echo("error: speakups.db or history.db not found")
        raise typer.Exit(2)
    history = FrozenHistory(history_db_path())
    try:
        chat_ids: list[str] | None = None
        if chats:
            subjects = (
                {} if all(looks_like_chat_id(value) for value in chats)
                else asyncio.run(chat_subjects(history))
            )
            try:
                chat_ids = [resolve_chat(value, subjects) for value in chats]
            except ValueError as exc:
                typer.echo(f"error: --chat: {exc}")
                raise typer.Exit(2) from exc
        until_ms = int(time.time() * 1000)
        opportunities = load_opportunities(
            speakups, since_ms=until_ms - days * 86_400_000, until_ms=until_ms, chat_ids=chat_ids,
        )
        if limit is not None:
            opportunities = opportunities[-limit:]
        setup = _participation_replay_setup(config)
        judge = None
        if not no_judge:
            from yeoman_gateway.evaluation.judge_format import build_eval_client
            from yeoman_gateway.processing.model_route import RouteUnavailableError

            try:
                client = UsageCapture(build_eval_client(
                    config, route=None if profile else route, profile=profile,
                ))
            except RouteUnavailableError as exc:
                typer.echo(f"error: {exc}")
                raise typer.Exit(2) from exc
            judge = ParticipationJudge(
                client=client, allowed_emojis=setup.emojis, max_input_tokens=setup.max_input,
                timeout_seconds=float(participation.judge_timeout_seconds),
                max_output_tokens=int(participation.judge_max_output_tokens),
            )
            typer.echo(f"model calls: up to {2 * len(opportunities)} ({concurrency} in parallel)")

        records = asyncio.run(replay(
            opportunities, history=history, judge=judge, usage=captured_usage,
            concurrency=concurrency, **setup.replay_args,
        ))
    finally:
        history.close()
    summary = summarize(records, judged=judge is not None)
    directory = new_report_dir("replay", base=get_var_path().joinpath(*REPORT_DIR))
    write_outputs(directory, records, summary, render_markdown(summary, days=days))
    typer.echo(
        f"opportunities={summary['opportunities']} built_both={summary['built_both']} "
        f"agreement={summary.get('agreement_rate')} report: {directory}"
    )


def _decision_line(variant: dict) -> str:
    if variant.get("status") != "decided":
        return f"{variant.get('status')} {variant.get('error') or ''}".strip()
    parts = [str(variant.get("action"))]
    if variant.get("emoji"):
        parts.append(str(variant["emoji"]))
    if variant.get("reason"):
        parts.append(f"reason: {variant['reason']}")
    return " ".join(parts)


def _echo_label_summary(labels: dict[str, str]) -> None:
    from yeoman_gateway.evaluation.participation_context_ab import label_summary

    stats = label_summary(labels)
    share = stats["b_better_or_same_share"]
    verdict = "n/a" if share is None else ("met" if stats["meets_threshold"] else "not met")
    typer.echo(
        f"labelled={stats['labelled']} a_better={stats['a_better']} b_better={stats['b_better']} "
        f"same={stats['same']} b_better_or_same={share} "
        f"(threshold {stats['threshold']:.0%}: {verdict})"
    )


@eval_app.command("participation-context-review")
def eval_participation_context_review(
    report_dir: Path = typer.Argument(..., help="A participation-context replay report directory"),
    every: bool = typer.Option(
        False, "--all", help="Every disagreement, not only the summary's sample of up to 30"
    ),
    summary_only: bool = typer.Option(
        False, "--summary", help="Print the label counts only; no review"
    ),
) -> None:
    """Owner labelling of A/B disagreements; message text is shown here only, never written."""
    import json
    from datetime import UTC, datetime

    from yeoman_gateway.evaluation.participation_context_ab import (
        LABEL_KEYS,
        LABELS_FILE,
        FrozenHistory,
        append_label,
        chat_subjects,
        disagreement_ids,
        read_labels,
        rebuild_views,
    )

    directory = report_dir.expanduser()
    records_path = directory / "records.jsonl"
    if not records_path.is_file() or not (directory / "summary.json").is_file():
        typer.echo("error: not a replay report (records.jsonl / summary.json missing)")
        raise typer.Exit(2)
    labels_path = directory / LABELS_FILE
    labels = read_labels(labels_path)
    if summary_only:
        _echo_label_summary(labels)
        return
    records = [json.loads(line) for line in records_path.read_text(encoding="utf-8").splitlines()
               if line.strip()]
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    by_id = {str(record["opportunity_id"]): record for record in records}
    todo = [item for item in disagreement_ids(records, summary, every=every)
            if item in by_id and item not in labels]
    if not todo:
        typer.echo("nothing to label")
        _echo_label_summary(labels)
        return

    from yeoman_shared.config.loader import load_config

    from yeoman_gateway.history.live import history_db_path

    if not history_db_path().is_file():
        typer.echo("error: history.db not found")
        raise typer.Exit(2)
    setup = _participation_replay_setup(load_config())
    history = FrozenHistory(history_db_path())
    prompt = "[a] A better / [b] B better / [s] same / [k] skip / [q] quit"
    try:
        subjects = asyncio.run(chat_subjects(history))
        for index, opportunity_id in enumerate(todo, start=1):
            record = by_id[opportunity_id]
            views = asyncio.run(rebuild_views(record, history=history, **setup.replay_args))
            moment = datetime.fromtimestamp(int(record["at_ms"]) / 1000).astimezone()
            typer.echo("")
            typer.echo(f"=== {index}/{len(todo)} {opportunity_id}")
            typer.echo(f"chat: {subjects.get(str(record.get('chat_id')), '(no subject)')}")
            typer.echo(f"time: {moment:%Y-%m-%d %H:%M %Z}")
            for key, name in (("A", "window"), ("B", "conversation")):
                typer.echo(f"--- {key} ({name}) context")
                typer.echo(views[key])
            for key in ("A", "B"):
                typer.echo(f"{key} decision: {_decision_line(record.get(key) or {})}")
            while True:
                try:
                    answer = typer.prompt(prompt).strip().lower()
                except typer.Abort:
                    answer = "q"
                if answer in {*LABEL_KEYS, "k", "q"}:
                    break
                typer.echo("please answer a, b, s, k or q")
            if answer == "q":
                break
            if answer == "k":
                continue
            append_label(labels_path, opportunity_id, LABEL_KEYS[answer],
                         labelled_at=datetime.now(UTC).isoformat(timespec="seconds"))
            labels[opportunity_id] = LABEL_KEYS[answer]
    finally:
        history.close()
    _echo_label_summary(labels)
