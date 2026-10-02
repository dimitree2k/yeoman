"""Judge output-format harness (V1 spec §4.4, D1): failure rate and latency by mode.

Runs the real Judge on synthetic scenarios against a configured route or an in-memory
profile override. Optional raw records contain only synthetic scenario content.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yeoman_gateway.evaluation.harness import RecordingClient, build_today_judge_context, make_judge
from yeoman_gateway.evaluation.scenarios import Scenario
from yeoman_gateway.processing.model_route import RouteClient, RouteUnavailableError
from yeoman_gateway.processing.participation import ParticipationDecisionError

EVAL_ROUTE = "eval.judge"
_PLAIN_CLASSES = {"empty_response", "provider_error", "timeout", "context_too_large"}


def build_eval_client(
    config: Any,
    *,
    route: str | None = None,
    profile: str | None = None,
    structured_output: str | None = None,
) -> RouteClient:
    """Resolve an evaluation route from a deep copy of configuration."""
    copied = config.model_copy(deep=True)
    if profile is not None:
        if profile not in copied.models.profiles:
            raise RouteUnavailableError(f"unknown profile '{profile}'")
        copied.models.routes[EVAL_ROUTE] = profile
        route = EVAL_ROUTE
        if structured_output is not None:
            copied.models.profiles[profile].structured_output = structured_output
    elif route is None:
        raise RouteUnavailableError("give a route or a profile")
    elif structured_output is not None:
        profile_name = copied.models.routes.get(route)
        profile_config = copied.models.profiles.get(profile_name)
        if profile_config is not None:
            profile_config.structured_output = structured_output
    return RouteClient(config=copied, route_key=route)


def failure_class(error: ParticipationDecisionError) -> str:
    if error.reason in _PLAIN_CLASSES:
        return error.reason
    if error.detail in {"output_truncated", "not_json_object"}:
        return error.detail
    return f"schema_invalid:{error.detail or error.reason}"


@dataclass(frozen=True, slots=True)
class FormatSummary:
    route: str
    model: str
    structured_output: str
    attempts: int
    failures: int
    failure_rate: float
    by_class: dict[str, int]
    latency_ms_p50: int
    latency_ms_p90: int


async def run_judge_format(
    client: Any,
    scenarios: list[Scenario],
    *,
    runs: int,
    allowed_emojis: tuple[str, ...],
    record_dir: Path | None = None,
) -> FormatSummary:
    recorder = RecordingClient(client)
    judge = make_judge(recorder, allowed_emojis=allowed_emojis)
    attempts = failures = 0
    by_class: dict[str, int] = {}
    latencies: list[int] = []
    for scenario in scenarios:
        for run in range(max(1, runs)):
            attempts += 1
            recorder.last = None
            opportunity, context, _ = await build_today_judge_context(scenario)
            try:
                decision = await judge.decide(opportunity, context)
                outcome: dict[str, Any] = {"outcome": "decision", "action": decision.action}
            except ParticipationDecisionError as exc:
                failures += 1
                cls = failure_class(exc)
                by_class[cls] = by_class.get(cls, 0) + 1
                outcome = {"outcome": "error", "class": cls}
            reply = recorder.last
            if reply is not None:
                latencies.append(int(reply.latency_ms))
            if record_dir is not None:
                record_dir.mkdir(parents=True, exist_ok=True)
                (record_dir / f"{scenario.id}-{run:03d}.json").write_text(
                    json.dumps(
                        {
                            **outcome,
                            "scenario": scenario.id,
                            "run": run,
                            "raw": reply.content if reply else "",
                            "finish_reason": reply.finish_reason if reply else "",
                            "diagnostics": dict(reply.diagnostics) if reply else {},
                            "usage": dict(reply.usage) if reply else {},
                            "latency_ms": reply.latency_ms if reply else None,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
    latencies.sort()
    def pick(q: float) -> int:
        return latencies[min(len(latencies) - 1, int(len(latencies) * q))] if latencies else 0
    return FormatSummary(
        route=str(getattr(client, "route_key", "")),
        model=str(getattr(client, "model", "")),
        structured_output=str(getattr(client, "structured_output", "")),
        attempts=attempts,
        failures=failures,
        failure_rate=round(failures / attempts, 4) if attempts else 0.0,
        by_class=by_class,
        latency_ms_p50=pick(0.5),
        latency_ms_p90=pick(0.9),
    )
