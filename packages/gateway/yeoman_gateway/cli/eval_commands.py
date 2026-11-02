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
        "conversation_min_messages": int(participation.conversation_min_messages),
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
    from_report: Path | None = typer.Option(
        None, "--from-report",
        help="Replay exactly the opportunities of this earlier report (ignores --days and "
        "--limit; --chat still filters); checks new B against its labels.jsonl",
    ),
    judge_repeats: int = typer.Option(
        1, "--judge-repeats", min=1, max=10,
        help="Ask the B Judge N times per opportunity; the majority decides",
    ),
    with_state: bool = typer.Option(
        False, "--with-state",
        help="Also measure the [Group State] block (S) and a B/B2 noise floor",
    ),
) -> None:
    """Replay recorded Judge opportunities with the window (A) and conversation (B) views."""
    import time

    from yeoman_shared.config.loader import load_config
    from yeoman_shared.utils.helpers import get_operational_store_path, get_var_path

    from yeoman_gateway.evaluation.participation_context_ab import (
        LABELS_FILE,
        REPORT_DIR,
        FrozenHistory,
        UsageCapture,
        background_reader,
        captured_usage,
        chat_subjects,
        label_check,
        load_opportunities,
        looks_like_chat_id,
        read_labels,
        read_only_source_readers,
        read_records,
        record_opportunity,
        render_markdown,
        replay,
        resolve_chat,
        summarize,
        write_outputs,
    )
    from yeoman_gateway.evaluation.report import new_report_dir
    from yeoman_gateway.history.live import history_db_path
    from yeoman_gateway.processing.participation import ParticipationJudge

    source_dir = from_report.expanduser() if from_report is not None else None
    source_records: list[dict] = []
    if source_dir is not None:
        if not (source_dir / "records.jsonl").is_file():
            typer.echo("error: --from-report: records.jsonl missing")
            raise typer.Exit(2)
        source_records = read_records(source_dir / "records.jsonl")
    config = load_config()
    participation = config.processing.participation
    speakups = get_operational_store_path("speakups", create=False)
    if (source_dir is None and not speakups.is_file()) or not history_db_path().is_file():
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
        if source_dir is not None:
            # The same opportunity ids in the same order; --days/--limit do not apply.
            wanted = None if chat_ids is None else set(chat_ids)
            opportunities = [
                record_opportunity(record) for record in source_records
                if wanted is None or str(record.get("chat_id")) in wanted
            ]
        else:
            until_ms = int(time.time() * 1000)
            opportunities = load_opportunities(
                speakups, since_ms=until_ms - days * 86_400_000, until_ms=until_ms,
                chat_ids=chat_ids,
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
            calls = (judge_repeats + 2 if with_state else 1 + judge_repeats) * len(
                opportunities
            )
            typer.echo(f"model calls: up to {calls} ({concurrency} in parallel)")

        state_for = None
        if with_state:
            from yeoman_gateway.app.bootstrap import _processing_store_path
            from yeoman_gateway.group_state.as_of import (
                UNAVAILABLE_IN_REPLAY,
                build_state_as_of,
            )
            from yeoman_gateway.group_state.contract import ContributorRegistry
            from yeoman_gateway.group_state.render import render_judge_view
            from yeoman_gateway.group_state.sections import default_contributors
            from yeoman_gateway.history.context import current_history_snapshot
            from yeoman_gateway.history.queries import HistoryQueries

            registry = ContributorRegistry()
            for contributor in default_contributors():
                registry.register(contributor)
            sources = read_only_source_readers(
                speakups_db=speakups,
                processing_db=_processing_store_path(config),
                decisions_db=get_operational_store_path("group_decisions", create=False),
            )

            async def state_for(item):
                snapshot = current_history_snapshot()
                queries = HistoryQueries(snapshot) if snapshot is not None else None
                state = await build_state_as_of(
                    registry=registry,
                    channel=item.channel,
                    chat_id=item.chat_id,
                    at_ms=item.at_ms,
                    history=queries,
                    sources=sources,
                    settings=config.processing.group_state,
                    consciousness=config.consciousness,
                )
                return render_judge_view(
                    state,
                    reader=background_reader(item),
                    now_ms=item.at_ms,
                    unavailable=UNAVAILABLE_IN_REPLAY,
                )

        records = asyncio.run(replay(
            opportunities, history=history, judge=judge, usage=captured_usage,
            concurrency=concurrency, judge_repeats=judge_repeats, state_for=state_for,
            **setup.replay_args,
        ))
    finally:
        history.close()
    summary = summarize(records, judged=judge is not None)
    if source_dir is not None:
        summary["source_report"] = source_dir.name
        labels = read_labels(source_dir / LABELS_FILE)
        if labels and judge is not None:
            summary["label_check"] = label_check(records, source_records, labels)
    directory = new_report_dir("replay", base=get_var_path().joinpath(*REPORT_DIR))
    markdown = render_markdown(
        summary, days=days, source_report=source_dir.name if source_dir is not None else None
    )
    write_outputs(directory, records, summary, markdown)
    check = summary.get("label_check")
    estimate = (
        f" label_check={check['estimated_b_better_or_same_share']} of {check['checked']}"
        if check else ""
    )
    typer.echo(
        f"opportunities={summary['opportunities']} built_both={summary['built_both']} "
        f"agreement={summary.get('agreement_rate')}{estimate} report: {directory}"
    )


@eval_app.command("reply-state")
def eval_reply_state(
    days: int = typer.Option(30, "--days", min=1, max=90),
    replies: int = typer.Option(200, "--replies", min=0),
    writer: int = typer.Option(80, "--writer", min=0),
    seed: int = typer.Option(7, "--seed"),
    oracle: bool = typer.Option(False, "--oracle", help="Also run the oracle-label arm Sx"),
    yes: bool = typer.Option(False, "--yes", help="Actually spend model calls"),
    limit_calls: int | None = typer.Option(
        None, "--limit-calls", min=1, help="Abort cleanly after N calls; write a partial report"
    ),
    chats: list[str] | None = typer.Option(
        None, "--chat", help="Only this chat (id or exact group subject); repeatable"
    ),
    concurrency: int = typer.Option(
        1, "--concurrency", min=1, max=8, help="Items evaluated at the same time"
    ),
    arms: str = typer.Option(
        "A,A2,R,P_R",
        "--arms",
        help=(
            "Comma list of arms: A, A2, R, P_R, N, P_N, H, P_H and the older S, P, Sx"
        ),
    ),
    measure: bool = typer.Option(
        False,
        "--measure",
        help="Write measure.json with counts only; no generation and no grading",
    ),
) -> None:
    """Offline reply-state evaluation: as-of prompts, four arms and a blind grader."""
    from yeoman_shared.config.loader import load_config
    from yeoman_shared.utils.helpers import get_operational_store_path

    from yeoman_gateway.evaluation.participation_context_ab import (
        FrozenHistory,
        chat_subjects,
        looks_like_chat_id,
        resolve_chat,
    )
    from yeoman_gateway.evaluation.reply_state_run import (
        ARMS,
        GRADER_ROUTE,
        REPLY_ROUTE,
        WRITER_ROUTE,
        ReplyStateRunner,
        RunConfig,
    )
    from yeoman_gateway.history.live import history_db_path

    canonical_arms = {arm.upper(): arm for arm in ARMS}
    requested_arms = tuple(
        dict.fromkeys(
            canonical_arms.get(part.strip().upper(), part.strip().upper())
            for part in str(arms).split(",")
            if part.strip()
        )
    )
    unknown_arms = sorted(set(requested_arms) - set(ARMS))
    if unknown_arms or not requested_arms:
        typer.echo(f"error: --arms: unknown arm(s) {', '.join(unknown_arms) or '(empty)'}")
        raise typer.Exit(2)
    oracle = bool(oracle or "Sx" in requested_arms)
    config = load_config()
    if not history_db_path().is_file():
        typer.echo("error: history.db not found")
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
        setup = _participation_replay_setup(config)
        from yeoman_gateway.agent.context import ContextBuilder
        from yeoman_gateway.app.bootstrap import _processing_store_path

        mode = str(config.processing.participation.context_mode)
        bounds = setup.replay_args[
            "conversation_bounds" if mode == "conversation" else "window_bounds"
        ]
        from .core import make_policy_engine

        engine, _policy_path = make_policy_engine(config)

        from yeoman_gateway.knowledge import open_knowledge_store, workspace_id_for
        from yeoman_gateway.knowledge.runtime import (
            RuntimeKnowledgePolicy,
            RuntimeKnowledgeSources,
        )
        from yeoman_gateway.people.resolver import PeopleResolver
        from yeoman_gateway.people.schema import people_db_path

        people = PeopleResolver(people_db_path(create=False))
        knowledge = None
        if bool(config.knowledge.enabled) and config.knowledge.resolved_db_path.is_file():
            try:
                knowledge = open_knowledge_store(
                    config.knowledge.resolved_db_path,
                    workspace_id=workspace_id_for(config.workspace_path),
                    source_authority=RuntimeKnowledgeSources(),
                    policy_authority=RuntimeKnowledgePolicy(engine=engine, policy_revision=1),
                    create=False,
                    readonly=True,
                    people=people,
                )
            except Exception as exc:  # noqa: BLE001 - the R arm degrades to no recall
                typer.echo(
                    f"warning: knowledge recall unavailable: {type(exc).__name__}"
                )

        def persona_for(channel: str, chat_id: str) -> str | None:
            try:
                policy = engine.resolve_policy(channel, chat_id)
                return engine.persona_text(policy.persona_file)
            except Exception:  # noqa: BLE001 - a missing persona is simply no persona
                return None

        run = RunConfig(
            days=days, replies=replies, writer=writer, seed=seed, oracle=oracle,
            limit_calls=limit_calls, yes=yes, concurrency=concurrency,
            arms=requested_arms,
        )
        runner = ReplyStateRunner(
            config=config, history=history,
            context_builder=ContextBuilder(config.workspace_path),
            participation_builder=setup.replay_args["builder"],
            bounds=bounds,
            guidance_for=setup.replay_args["guidance_for"],
            persona_for=persona_for,
            processing_db=_processing_store_path(config),
            speakups_db=get_operational_store_path("speakups", create=False),
            decisions_db=get_operational_store_path("group_decisions", create=False),
            knowledge=knowledge,
            people=people,
        )
        prepared = asyncio.run(runner.prepare(run, chat_ids=chat_ids))
        planned = prepared.planned
        typer.echo(
            f"items: {planned.items} (reply {len(prepared.replies)}, "
            f"writer {len(prepared.writer)}); shortfall "
            f"{dict(prepared.selection.shortfall)}"
        )
        for route, count in planned.routes.items():
            typer.echo(f"calls {route}: {count}")
        typer.echo(
            f"token estimate: prompt={planned.prompt_tokens} "
            f"completion={planned.completion_tokens}"
        )
        if measure:
            measured = asyncio.run(runner.measure(prepared, run))
            typer.echo(f"measure: {measured.report_dir}")
            return
        if not yes:
            typer.echo("no model calls; rerun with --yes")
            return

        from yeoman_gateway.evaluation.long_term_context import (
            PEOPLE_ROUTE,
            SUMMARIES_ROUTE,
        )
        from yeoman_gateway.processing.model_route import RouteClient, RouteUnavailableError

        try:
            clients = {
                REPLY_ROUTE: RouteClient(config=config, route_key=REPLY_ROUTE),
                WRITER_ROUTE: RouteClient(config=config, route_key=WRITER_ROUTE),
                str(config.processing.group_state.labels.route): RouteClient(
                    config=config, route_key=str(config.processing.group_state.labels.route)
                ),
                GRADER_ROUTE: RouteClient(config=config, route_key=GRADER_ROUTE),
            }
            if any(arm in {"N", "P_N"} for arm in requested_arms):
                clients[PEOPLE_ROUTE] = RouteClient(config=config, route_key=PEOPLE_ROUTE)
            if any(arm in {"H", "P_H"} for arm in requested_arms):
                clients[SUMMARIES_ROUTE] = RouteClient(
                    config=config, route_key=SUMMARIES_ROUTE
                )
        except RouteUnavailableError as exc:
            typer.echo(f"error: model route unavailable: {exc}")
            raise typer.Exit(2) from exc
        runner.set_clients(clients)
        outcome = asyncio.run(runner.execute(prepared, run))
    finally:
        history.close()
        if knowledge is not None:
            knowledge.close()
    summary = outcome.summary or {}
    gate = summary.get("gate") or {}
    typer.echo(
        f"partial={summary.get('partial')} s_gt_a={gate.get('s_gt_a')} "
        f"s_gt_p={gate.get('s_gt_p')} r_gt_a={gate.get('r_gt_a')} "
        f"r_gt_p_r={gate.get('r_gt_p_r')} report: {outcome.report_dir}"
    )


@eval_app.command("reply-state-review")
def eval_reply_state_review(
    report_dir: Path = typer.Argument(..., help="A reply-state evaluation report directory"),
    sample: int = typer.Option(20, "--sample", min=1, max=200),
    seed: int = typer.Option(7, "--seed"),
    pair: str = typer.Option(
        "S_vs_A", "--pair", help="Pair to review, e.g. N_vs_A, H_vs_A, A2_vs_A"
    ),
    stratum: str | None = typer.Option(
        None,
        "--stratum",
        help="Only this stratum; 'older_context' selects structural older-context items",
    ),
    out: Path = typer.Option(..., "--out", help="Owner review markdown (mode 0600)"),
) -> None:
    """Write the blind owner review and its separate X/Y key."""
    from yeoman_gateway.evaluation.participation_context_ab import FrozenHistory
    from yeoman_gateway.evaluation.reply_state_run import build_review, write_review
    from yeoman_gateway.history.live import history_db_path

    directory = report_dir.expanduser()
    if not (directory / "items.jsonl").is_file():
        typer.echo("error: not a reply-state report (items.jsonl missing)")
        raise typer.Exit(2)
    if not (directory / "candidates.jsonl").is_file():
        typer.echo("error: not a reply-state report (candidates.jsonl missing)")
        raise typer.Exit(2)
    if not history_db_path().is_file():
        typer.echo("error: history.db not found")
        raise typer.Exit(2)
    history = FrozenHistory(history_db_path())
    try:
        review, key = asyncio.run(
            build_review(
                directory,
                history,
                sample=sample,
                seed=seed,
                pair=pair,
                stratum=stratum,
            )
        )
    finally:
        history.close()
    key_path = write_review(out, review, key)
    typer.echo(f"review: {Path(out).expanduser()} ({len(key['items'])} items)")
    typer.echo(f"key: {key_path}")


def _decision_line(variant: dict) -> str:
    if variant.get("status") != "decided":
        return f"{variant.get('status')} {variant.get('error') or ''}".strip()
    parts = [str(variant.get("action"))]
    if variant.get("emoji"):
        parts.append(str(variant["emoji"]))
    if variant.get("action_distribution"):
        spread = ", ".join(f"{key} {value}" for key, value in variant["action_distribution"].items())
        parts.append(f"({spread} of {variant.get('judge_repeats')})")
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
        read_records,
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
    records = read_records(records_path)
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


def _groupnotes_replay_args(setup, context_mode: str) -> dict:
    """Spec A's setup reduced to one view: the live context mode."""
    args = dict(setup.replay_args)
    window = args.pop("window_bounds")
    conversation = args.pop("conversation_bounds")
    args["bounds"] = conversation if context_mode == "conversation" else window
    return args


def _digest_runner_for(config, history):
    """A digest runner per week over an in-memory sink; registry threads as of week end."""
    from yeoman_gateway.app.bootstrap import _processing_store_path
    from yeoman_gateway.evaluation.participation_context_ab import registry_lookup
    from yeoman_gateway.evaluation.participation_groupnotes_replay import MemoryDigestSink
    from yeoman_gateway.processing.group_digest import DigestBounds, GroupDigestRunner

    lookup = registry_lookup(_processing_store_path(config))
    bounds = DigestBounds.from_config(config)

    def _no_client():
        raise AssertionError("the replay passes its digest client explicitly")

    def runner_for(week):
        return GroupDigestRunner(
            knowledge=MemoryDigestSink(), history=history, bounds=bounds,
            client_factory=_no_client,
            thread_lookup=(lambda native: lookup(native, week.end_ms - 1)) if lookup else None,
        )

    return runner_for


@eval_app.command("participation-groupnotes")
def eval_participation_groupnotes(
    days: int = typer.Option(14, "--days", min=1, max=90),
    no_judge: bool = typer.Option(
        False, "--no-judge",
        help="No model calls: stored notes only (else 'note unavailable'), token estimates",
    ),
    route: str = typer.Option("participation.judge", "--route"),
    profile: str | None = typer.Option(None, "--profile"),
    limit: int | None = typer.Option(None, "--limit", min=1, help="Newest N opportunities only"),
    concurrency: int = typer.Option(6, "--concurrency", min=1, help="Parallel model calls"),
    chats: list[str] | None = typer.Option(
        None, "--chat", help="Only this chat (id or exact group subject); repeatable"
    ),
    from_report: Path | None = typer.Option(
        None, "--from-report",
        help="Replay exactly the opportunities (and notes) of this earlier report (ignores "
        "--days and --limit; --chat still filters); checks new B against its labels.jsonl",
    ),
    judge_repeats: int = typer.Option(
        1, "--judge-repeats", min=1, max=10,
        help="Ask the B Judge N times per opportunity; the majority decides",
    ),
    use_stored: bool = typer.Option(
        False, "--use-stored",
        help="Reuse a stored digest of the chat-week when there is one (default: generate)",
    ),
) -> None:
    """Replay recorded group Judge opportunities without (A) and with (B) group notes.

    The note of an opportunity is the one of the ISO week before its own, generated in
    memory from that week only (never stored); one digest call per chat-week.
    """
    import time

    from yeoman_shared.config.loader import load_config
    from yeoman_shared.utils.helpers import get_operational_store_path, get_var_path

    from yeoman_gateway.evaluation.participation_context_ab import (
        LABELS_FILE,
        FrozenHistory,
        UsageCapture,
        captured_usage,
        chat_subjects,
        label_check,
        load_opportunities,
        looks_like_chat_id,
        read_labels,
        read_records,
        record_opportunity,
        resolve_chat,
    )
    from yeoman_gateway.evaluation.participation_groupnotes_replay import (
        NOTES_FILE,
        REPORT_DIR,
        ReplayNotes,
        read_notes,
        render_markdown,
        replay,
        stored_note_reader,
        summarize,
        write_outputs,
    )
    from yeoman_gateway.evaluation.report import new_report_dir
    from yeoman_gateway.history.live import history_db_path
    from yeoman_gateway.processing.participation import ParticipationJudge

    source_dir = from_report.expanduser() if from_report is not None else None
    source_records: list[dict] = []
    if source_dir is not None:
        if not (source_dir / "records.jsonl").is_file():
            typer.echo("error: --from-report: records.jsonl missing")
            raise typer.Exit(2)
        source_records = read_records(source_dir / "records.jsonl")
    config = load_config()
    participation = config.processing.participation
    context_mode = str(participation.context_mode)
    speakups = get_operational_store_path("speakups", create=False)
    if (source_dir is None and not speakups.is_file()) or not history_db_path().is_file():
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
        if source_dir is not None:
            wanted = None if chat_ids is None else set(chat_ids)
            opportunities = [
                record_opportunity(record) for record in source_records
                if wanted is None or str(record.get("chat_id")) in wanted
            ]
        else:
            until_ms = int(time.time() * 1000)
            opportunities = load_opportunities(
                speakups, since_ms=until_ms - days * 86_400_000, until_ms=until_ms,
                chat_ids=chat_ids,
            )
            if limit is not None:
                opportunities = opportunities[-limit:]
        setup = _participation_replay_setup(config)
        judge = None
        digest_client = None
        if not no_judge:
            from yeoman_gateway.evaluation.judge_format import build_eval_client
            from yeoman_gateway.processing.group_digest import DIGEST_ROUTE
            from yeoman_gateway.processing.model_route import RouteClient, RouteUnavailableError

            try:
                client = UsageCapture(build_eval_client(
                    config, route=None if profile else route, profile=profile,
                ))
                digest_client = RouteClient(config=config, route_key=DIGEST_ROUTE)
            except RouteUnavailableError as exc:
                typer.echo(f"error: {exc}")
                raise typer.Exit(2) from exc
            judge = ParticipationJudge(
                client=client, allowed_emojis=setup.emojis, max_input_tokens=setup.max_input,
                timeout_seconds=float(participation.judge_timeout_seconds),
                max_output_tokens=int(participation.judge_max_output_tokens),
            )
            typer.echo(
                f"model calls: up to {(1 + judge_repeats) * len(opportunities)} judge and one "
                f"digest per chat-week ({concurrency} in parallel)"
            )
        notes = ReplayNotes(
            runner_for=_digest_runner_for(config, history),
            client=digest_client,
            generate=not no_judge,
            stored=stored_note_reader(config.knowledge.resolved_db_path),
            use_stored=use_stored,
            seeded=read_notes(source_dir / NOTES_FILE) if source_dir is not None else None,
        )
        records = asyncio.run(replay(
            opportunities, history=history, notes=notes, judge=judge, usage=captured_usage,
            concurrency=concurrency, judge_repeats=judge_repeats,
            **_groupnotes_replay_args(setup, context_mode),
        ))
    finally:
        history.close()
    summary = summarize(records, notes.notes(), judged=judge is not None,
                        context_mode=context_mode)
    if source_dir is not None:
        summary["source_report"] = source_dir.name
        labels = read_labels(source_dir / LABELS_FILE)
        if labels and judge is not None:
            summary["label_check"] = label_check(records, source_records, labels)
    directory = new_report_dir("replay", base=get_var_path().joinpath(*REPORT_DIR))
    markdown = render_markdown(
        summary, days=days, source_report=source_dir.name if source_dir is not None else None
    )
    write_outputs(directory, records, summary, markdown, notes.notes())
    typer.echo(
        f"opportunities={summary['opportunities']} built_both={summary['built_both']} "
        f"digest_calls={summary['notes']['digest_calls']} "
        f"agreement={summary.get('agreement_rate')} report: {directory}"
    )


@eval_app.command("participation-outcomes")
def eval_participation_outcomes(
    switch: str = typer.Option(..., "--switch", help="ISO 8601 timestamp including timezone"),
    days: int = typer.Option(14, "--days", min=1, max=365),
    chats: list[str] | None = typer.Option(None, "--chat", help="Chat id or exact group subject; repeatable"),
    output_dir: Path | None = typer.Option(None, "--output-dir"),
) -> None:
    """Compare recorded Participation outcomes in equal windows around a switch time."""
    import json
    import sqlite3
    from datetime import UTC, datetime

    from yeoman_shared.config.defaults import DEFAULT_PROCESSING_DB_PATH
    from yeoman_shared.config.loader import convert_keys
    from yeoman_shared.config.schema import Config
    from yeoman_shared.utils.helpers import (
        OPERATIONAL_STORE_PATHS,
        get_yeoman_home,
        resolve_yeoman_path,
    )

    from yeoman_gateway.evaluation.participation_context_ab import (
        looks_like_chat_id,
        resolve_chat,
    )
    from yeoman_gateway.evaluation.participation_outcomes import (
        evaluate,
        render_markdown,
        write_outputs,
    )
    from yeoman_gateway.history.live import HistoryBoundary, HistoryPaused
    from yeoman_gateway.history.queries import HistoryQueries
    from yeoman_gateway.history.reader import HistoryReader

    try:
        moment = datetime.fromisoformat(switch.replace("Z", "+00:00"))
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("timezone required")
    except ValueError as exc:
        typer.echo(f"error: --switch must be an ISO timestamp with timezone: {exc}")
        raise typer.Exit(2) from exc
    root = get_yeoman_home()
    data = root / "data"
    config_path = root / "config.json"
    config = Config.model_validate(convert_keys(json.loads(config_path.read_text(encoding="utf-8")))) \
        if config_path.is_file() else Config()
    history_db = data / "history" / "history.db"
    processing_value = config.processing.db_path
    if processing_value in {DEFAULT_PROCESSING_DB_PATH, "data/processing/processing.db"}:
        processing_db = data / OPERATIONAL_STORE_PATHS["processing"]
    else:
        configured_path = resolve_yeoman_path(processing_value)
        processing_db = configured_path if configured_path.is_absolute() else root / configured_path
    speakups_db = data / OPERATIONAL_STORE_PATHS["speakups"]
    if not all(path.is_file() for path in (history_db, processing_db, speakups_db)):
        typer.echo("error: history.db, processing.db, or speakups.db not found")
        raise typer.Exit(2)

    chat_ids: set[str] | None = None
    if chats:
        chat_ids = set()
        subjects: dict[str, str] = {}
        if not all(looks_like_chat_id(value) for value in chats):
            reader = HistoryReader(history_db)
            try:
                conn = sqlite3.connect(
                    f"{history_db.resolve().as_uri()}?mode=ro", uri=True
                )
                try:
                    row = conn.execute(
                        "SELECT state_json FROM projector_state WHERE file='@runtime'"
                    ).fetchone()
                finally:
                    conn.close()
                state = json.loads(row[0]) if row else None
                if not isinstance(state, dict) or not isinstance(state.get("generation"), int):
                    raise HistoryPaused("reader_unavailable")
                snapshot = reader.open_snapshot(HistoryBoundary(state["generation"], ()))
                try:
                    subjects = {str(row["chat_id"]): str(row["subject"])
                                for row in HistoryQueries(snapshot).chats() if row.get("subject")}
                finally:
                    snapshot.close()
            finally:
                reader.close()
        try:
            chat_ids = {resolve_chat(value, subjects) for value in chats}
        except ValueError as exc:
            typer.echo(f"error: --chat: {exc}")
            raise typer.Exit(2) from exc

    directory = output_dir.expanduser() if output_dir is not None else (
        root / "var" / "eval" / "participation-outcomes" /
        datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    )
    try:
        summary = evaluate(
            history_db=history_db, processing_db=processing_db, speakups_db=speakups_db,
            switch=moment, days=days,
            lull_minutes=int(config.consciousness.lull_silence_minutes), chats=chat_ids,
        )
        write_outputs(directory, summary)
    except (OSError, sqlite3.Error, ValueError, HistoryPaused) as exc:
        typer.echo(f"error: participation outcomes: {exc}")
        raise typer.Exit(2) from exc
    typer.echo(render_markdown(summary))
    typer.echo(f"partial={summary['partial']} report: {directory}")


@eval_app.command("participation-groupnotes-review")
def eval_participation_groupnotes_review(
    report_dir: Path = typer.Argument(..., help="A participation-groupnotes replay report directory"),
    every: bool = typer.Option(
        False, "--all", help="Every disagreement, not only the summary's sample of up to 30"
    ),
    summary_only: bool = typer.Option(
        False, "--summary", help="Print the label counts only; no review"
    ),
) -> None:
    """Owner labelling of without/with-note disagreements; text is shown here only."""
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
        read_records,
    )
    from yeoman_gateway.evaluation.participation_groupnotes_replay import (
        NOTES_FILE,
        note_block,
        read_notes,
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
    records = read_records(records_path)
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    notes = read_notes(directory / NOTES_FILE)
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
    args = _groupnotes_replay_args(setup, str(summary.get("context_mode") or "window"))
    history = FrozenHistory(history_db_path())
    prompt = "[a] A better / [b] B better / [s] same / [k] skip / [q] quit"
    try:
        subjects = asyncio.run(chat_subjects(history))
        for index, opportunity_id in enumerate(todo, start=1):
            record = by_id[opportunity_id]
            meta = record.get("note") or {}
            note = notes.get((str(meta.get("chat_id")), str(meta.get("week"))))
            if note is None:
                typer.echo(f"=== {opportunity_id}: note missing from {NOTES_FILE}; skipped")
                continue
            views = asyncio.run(rebuild_views(
                record, note, history=history, builder=args["builder"], bounds=args["bounds"],
                estimator=args["estimator"], guidance_for=args["guidance_for"],
                set_as_of=args["set_as_of"],
            ))
            a_lines = views["A"].splitlines()
            b_lines = views["B"].splitlines()
            moment = datetime.fromtimestamp(int(record["at_ms"]) / 1000).astimezone()
            typer.echo("")
            typer.echo(f"=== {index}/{len(todo)} {opportunity_id}")
            typer.echo(f"chat: {subjects.get(str(record.get('chat_id')), '(no subject)')}")
            typer.echo(f"time: {moment:%Y-%m-%d %H:%M %Z}")
            typer.echo("--- A (live context, no note)")
            typer.echo(views["A"])
            typer.echo(f"--- B = A plus the note of week {note.week}")
            if not record.get("B", {}).get("group_notes_rendered"):
                typer.echo("(the note was dropped by the budget)")
            typer.echo(note_block(note))
            dropped = [line for line in a_lines if line not in set(b_lines)]
            if dropped:
                typer.echo(f"(B drops {len(dropped)} line(s) of A to fit the budget)")
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


@eval_app.command("knowledge-gold")
def eval_knowledge_gold(
    gold: Path = typer.Option(..., "--gold"),
    knowledge_db: Path = typer.Option(..., "--knowledge-db"),
    people_db: Path = typer.Option(..., "--people-db"),
    history_db: Path = typer.Option(..., "--history-db"),
    out: Path = typer.Option(..., "--out"),
    grader_route: str | None = typer.Option(None, "--grader-route"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Measure Knowledge on private owner gold and closed, isolated database copies."""
    from yeoman_gateway.evaluation.knowledge_gold import (
        evaluate,
        load_gold,
        private_path,
        recall_items,
        write_outputs,
    )

    try:
        private_path(out)
        cases = load_gold(gold)
        contexts = recall_items(cases, knowledge_db=knowledge_db, people_db=people_db,
                                history_db=history_db)
        client = None
        if grader_route and not dry_run:
            from yeoman_shared.config.loader import load_config

            from yeoman_gateway.evaluation.judge_format import build_eval_client

            client = build_eval_client(load_config(), route=grader_route)
        report = asyncio.run(evaluate(cases, contexts, client=client))
        write_outputs(out, report)
    except Exception:
        # Input, SQL and provider exceptions may include private content; never echo them.
        typer.echo("knowledge-gold: evaluation unavailable or invalid input")
        raise typer.Exit(2) from None
    typer.echo("knowledge-gold: report saved")


@eval_app.command("knowledge-gold-template")
def eval_knowledge_gold_template(
    people_db: Path = typer.Option(..., "--people-db"),
    history_db: Path = typer.Option(..., "--history-db"),
    persons: str = typer.Option(..., "--persons"),
    out: Path = typer.Option(..., "--out"),
) -> None:
    """Write content-free case slots for coordinator/owner completion outside git."""
    from yeoman_gateway.evaluation.knowledge_gold import make_template

    try:
        make_template(people_db=people_db, history_db=history_db,
                      persons=[int(value.strip()) for value in persons.split(",")], out=out)
    except Exception:
        typer.echo("knowledge-gold-template: invalid input or unavailable copies")
        raise typer.Exit(2) from None
    typer.echo("knowledge-gold-template: skeleton saved; owner completion required")
