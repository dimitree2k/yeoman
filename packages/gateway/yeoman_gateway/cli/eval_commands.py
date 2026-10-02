"""Offline evaluation commands for synthetic Participation and retrieval scenarios."""

from __future__ import annotations

import asyncio
from dataclasses import asdict

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
