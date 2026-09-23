"""Offline evaluation of the short-reply path (runtime spec 2026-09-22 §10).

  replay  - rebuild replies to Arvid from the runtime databases (read-only)
  sample  - write the owner label sheet
  decide  - run the real reactor against the configured model (needs --send-to-provider)
  report  - metrics, usage and go/no-go as Markdown
"""

from __future__ import annotations

import argparse
import asyncio
import os
from datetime import datetime
from pathlib import Path

from yeoman_gateway.short_reply import evaluation as ev

RUNTIME = Path(os.environ.get("YEOMAN_HOME", Path.home() / ".yeoman"))
OUT = RUNTIME / "var" / "eval" / "short-reply"


def _replay(args: argparse.Namespace) -> None:
    since = int(datetime.fromisoformat(args.since).timestamp())
    rows = ev.load_replay_rows(
        RUNTIME / "data" / "inbound" / "reply_context.db",
        RUNTIME / "data" / "processing" / "processing.db",
        since_ts=since,
        inbound_jsonl=sorted((RUNTIME / "data" / "inbound").glob("*.jsonl")),
        max_chars=args.max_chars,
    )
    ev.rows_to_jsonl(rows, OUT / args.output)
    print(f"rows={len(rows)} -> {OUT / args.output}")


def _sample(args: argparse.Namespace) -> None:
    rows = ev.rows_from_jsonl(OUT / "rows.jsonl")
    sample = ev.sample_for_labels(rows, size=args.size, max_chars=args.max_chars)
    ev.write_label_sheet(sample, OUT / "labels.csv")
    print(f"sampled={len(sample)} of {len(rows)} -> {OUT / 'labels.csv'}")


def _all_rows(args: argparse.Namespace) -> tuple[list[ev.ReplayRow], dict[str, ev.Label]]:
    labels = ev.read_labels(OUT / "labels.csv")
    rows = ev.rows_from_jsonl(OUT / "rows.jsonl")
    if args.synthetic:
        synthetic_rows, synthetic_labels = ev.load_synthetic(Path(args.synthetic))
        rows += synthetic_rows
        labels.update(synthetic_labels)
    return rows, labels


def _decide(args: argparse.Namespace) -> None:
    if not args.send_to_provider:
        raise SystemExit("refusing: pass --send-to-provider after owner approval (Gate B)")
    from yeoman_gateway.processing.model_route import RouteClient
    from yeoman_gateway.short_reply.decider import ShortReplyDecider
    from yeoman_shared.config.loader import load_config

    config = load_config()
    settings = config.processing.short_reply.model_copy(
        update={"mode": "live", "max_chars": 100}
    )
    route = "reaction.decide"
    profile_name = config.models.routes.get(route)
    profile = config.models.profiles.get(profile_name or "")
    if (
        profile_name != "reaction_decide"
        or profile is None
        or profile.model != "openai/gpt-6-luna"
        or profile.provider != "openrouter"
    ):
        raise SystemExit("refusing: reaction.decide must use reactionDecide / openai/gpt-6-luna / openrouter")
    client = RouteClient(config=config, route_key=route)
    if client.model != "openai/gpt-6-luna":
        raise SystemExit("refusing: resolved model is not openai/gpt-6-luna")
    decider = ShortReplyDecider(
        client=client,
        allowed_emojis=tuple(config.processing.reaction_emojis),
        max_candidates=settings.max_candidates,
        timeout_seconds=settings.timeout_seconds,
        max_output_tokens=args.max_output_tokens or settings.max_output_tokens,
    )
    rows, labels = _all_rows(args)
    rows = [row for row in rows if row.row_id in labels]
    if args.limit:
        eligible = sorted(
            (row for row in rows if ev._row_is_candidate(row, max_chars=settings.max_chars)),
            key=lambda row: (row.timestamp, row.row_id),
        )
        rows = []
        min_gap_seconds = int(settings.rate_limit.window_seconds) + 1
        for row in eligible:
            if not rows or row.timestamp - rows[-1].timestamp >= min_gap_seconds:
                rows.append(row)
            if len(rows) == args.limit:
                break
        if len(rows) != args.limit:
            raise SystemExit(
                f"refusing: probe needs {args.limit} model-eligible labeled rows "
                f"spaced at least {min_gap_seconds}s apart for a clean rate-limit probe"
            )
    decisions = asyncio.run(ev.simulate_decisions(rows, decider, settings))
    ev.decisions_to_jsonl(decisions, OUT / args.output)
    print(f"model={client.model} decisions={len(decisions)} -> {OUT / args.output}")


def _report(args: argparse.Namespace) -> None:
    rows, labels = _all_rows(args)
    decisions = ev.decisions_from_jsonl(OUT / args.decisions)
    by_max_chars = {
        limit: ev.evaluate(rows, labels, decisions, max_chars=limit) for limit in (60, 80, 100)
    }
    usage = ev.usage_summary(
        decisions, price_in_per_mtok=args.price_in, price_out_per_mtok=args.price_out
    )
    media_rows = [row for row in rows if row.media_kind and not row.synthetic]
    media_coverage = (
        len(media_rows), sum(not row.media_metadata_available for row in media_rows)
    )
    since_ms = int(datetime.fromisoformat(args.since).timestamp() * 1000)
    coverage = ev.receipt_coverage(
        RUNTIME / "data" / "processing" / "processing.db", since_ms=since_ms,
        chat_id=args.shadow_chat,
    )
    probe_path = OUT / "probe.jsonl"
    probe = ev.decisions_from_jsonl(probe_path) if probe_path.exists() else []
    rollout = None
    if args.shadow_log and args.shadow_chat:
        shadow_rows_path = OUT / args.shadow_rows
        if not shadow_rows_path.exists():
            raise SystemExit(
                f"refusing: run `replay --since <shadow start> --output {args.shadow_rows}` first"
            )
        shadow_rows = [
            row for row in ev.rows_from_jsonl(shadow_rows_path)
            if row.timestamp >= since_ms // 1000
        ]
        shadow, expected, logged, dropped, days = ev.load_shadow_decisions(
            [Path(item) for item in args.shadow_log], shadow_rows, chat_id=args.shadow_chat,
        )
        processing_db = RUNTIME / "data" / "processing" / "processing.db"
        baseline_reactions, text_events = ev.load_effect_sequences(
            processing_db, since_ms=since_ms, chat_id=args.shadow_chat
        )
        row_by_id = {row.row_id: row for row in shadow_rows}
        shadow_reactions = [
            (row_by_id[item.row_id].chat_id, item.timestamp, item.chosen)
            for item in shadow if item.kind == "react" and item.chosen
        ]
        baseline = ev.sequence_metrics(baseline_reactions, text_events)
        shadow_sequence = ev.sequence_metrics(shadow_reactions, [])
        rollout = ev.RolloutMetrics(
            baseline=baseline,
            shadow=shadow_sequence,
            baseline_receipts_complete=all(
                sent == confirmed
                for sent, confirmed in (
                    coverage.get("send_reaction", (0, 0)),
                    coverage.get("send_text", (0, 0)),
                )
            ),
            offline_failures=sum(
                item.model_called and (bool(item.error) or item.source == "fallback")
                for item in decisions
            ),
            observed_days=days,
            expected_candidates=expected, logged_candidates=logged,
            shadow_dropped=dropped,
            provider_errors=sum(item.error == "provider_error" for item in shadow),
            invalid_json=sum(item.error == "invalid_json" for item in shadow),
            truncated=sum(item.error == "truncated_output" for item in shadow),
            fallbacks=sum(item.source == "fallback" for item in shadow),
        )
    path = OUT / "report.md"
    with ev._private_text_file(path) as handle:
        handle.write(
            ev.render_markdown(by_max_chars, usage, coverage, rollout, probe, media_coverage)
        )
    print(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    replay = sub.add_parser("replay")
    replay.add_argument("--since", default="2026-08-01")
    replay.add_argument("--max-chars", type=int, default=80)
    replay.add_argument("--output", default="rows.jsonl")
    replay.set_defaults(func=_replay)
    sample = sub.add_parser("sample")
    sample.add_argument("--size", type=int, default=150)
    sample.add_argument("--max-chars", type=int, default=80)
    sample.set_defaults(func=_sample)
    decide = sub.add_parser("decide")
    decide.add_argument("--synthetic")
    decide.add_argument("--limit", type=int, default=0)
    decide.add_argument("--max-output-tokens", type=int, default=0)
    decide.add_argument("--output", default="decisions.jsonl")
    decide.add_argument("--send-to-provider", action="store_true")
    decide.set_defaults(func=_decide)
    report = sub.add_parser("report")
    report.add_argument("--decisions", default="decisions.jsonl")
    report.add_argument("--synthetic")
    report.add_argument("--since", default="2026-09-11")
    report.add_argument("--shadow-log", action="append", default=[])
    report.add_argument("--shadow-chat")
    report.add_argument("--shadow-rows", default="shadow-rows.jsonl")
    report.add_argument("--max-chars", type=int, default=80)
    report.add_argument("--price-in", type=float)
    report.add_argument("--price-out", type=float)
    report.set_defaults(func=_report)
    args = parser.parse_args()
    OUT.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(OUT, 0o700)
    args.func(args)


if __name__ == "__main__":
    main()
