"""Synthetic retrieval evaluation against today's bounded Participation context."""

from __future__ import annotations

import json
import random
import statistics
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

from yeoman_gateway.evaluation.harness import build_today_judge_context
from yeoman_gateway.evaluation.scenarios import (
    Chat,
    Expectation,
    Message,
    Person,
    Scenario,
    Trigger,
)

RETRIEVAL_CATEGORIES: tuple[str, ...] = (
    "single_recent", "single_old", "cross_chat", "temporal", "update", "abstention",
)
NOW = datetime(2026, 10, 5, 18, 0, tzinfo=UTC)
NAMES = ("Frank", "Alex", "Kevin", "Lisa", "Mara", "Jonas")
CITIES = ("Köln", "Leipzig", "Bremen", "Freiburg", "Kassel", "Rostock", "Mainz", "Augsburg")
COMPANIES = ("Siemens", "DHL", "Bosch", "Zalando", "SAP", "Lidl", "BASF", "Otto")
SUBJECTS = ("Statistik", "Steuerrecht", "Anatomie", "Mathe", "BWL", "Chemie", "Jura", "Physik")
FILLERS = (
    "Hab heute endlich die Steuererklärung gemacht.", "Kennt jemand eine gute Pizzeria?",
    "Das Wetter ist echt mies heute.", "Hat jemand das Spiel gestern gesehen?",
    "Ich brauch dringend Urlaub.", "Neuer Rekord: 12 km gelaufen!",
    "Wer hat noch Lust auf Kino am Wochenende?", "Mein Laptop spinnt schon wieder.",
    "Ich war gestern im neuen Café, ganz okay.", "Hat jemand ein Ladekabel übrig?",
)


def _cast() -> tuple[dict[str, Person], dict[str, Chat]]:
    people = {
        name.lower(): Person(name.lower(), name, f"49170000010{index}@s.whatsapp.net")
        for index, name in enumerate(NAMES)
    }
    chats = {
        "g1": Chat("g1", "120000000000000011@g.us", ("frank", "alex", "kevin", "lisa", "mara")),
        "g2": Chat("g2", "120000000000000012@g.us", ("frank", "kevin", "jonas")),
        "g3": Chat("g3", "120000000000000013@g.us", ("alex", "lisa", "mara", "jonas", "frank")),
    }
    return people, chats


def _ms(at: datetime) -> int:
    return int(at.timestamp() * 1000)


def _distractors(rng: random.Random, sid: str, count: int, subject: str) -> list[Message]:
    people = [name.lower() for name in NAMES if name.lower() != subject]
    _, chats = _cast()
    out: list[Message] = []
    for index in range(count):
        chat = rng.choice(sorted(chats))
        members = [person for person in chats[chat].members if person in people] or people
        at = NOW - timedelta(minutes=rng.randint(130, 60 * 24 * 60))
        out.append(Message(
            f"{sid}-d{index}", chat, rng.choice(members), _ms(at), rng.choice(FILLERS),
        ))
    return out


def _scenario(
    sid: str,
    category: str,
    rng: random.Random,
    *,
    subject: str,
    trigger_chat: str,
    facts: list[Message],
    needed: tuple[str, ...],
    trigger_text: str,
    asker: str,
    abstain: bool = False,
) -> Scenario:
    people, chats = _cast()
    trigger = Message(f"{sid}-t", trigger_chat, asker, _ms(NOW - timedelta(minutes=1)), trigger_text)
    messages = sorted(
        [*_distractors(rng, sid, 30, subject), *facts, trigger], key=lambda message: message.at_ms,
    )
    return Scenario(
        id=sid,
        title=f"{category}: {subject}",
        category=category,
        modes=("A", "B"),
        requires=(),
        now_ms=_ms(NOW),
        people=people,
        chats=chats,
        messages=tuple(messages),
        trigger=Trigger(trigger_chat, "inbound", (trigger.id,)),
        expected=Expectation(needed_evidence=needed, abstain=abstain),
    )


def _s16() -> Scenario:
    rng = random.Random(16)
    facts = [
        Message("S16-home", "g1", "alex", _ms(NOW - timedelta(days=40)),
                "Ich wohne übrigens in Kassel-Wilhelmshöhe."),
        Message("S16-car", "g3", "alex", _ms(NOW - timedelta(days=10)),
                "Mein Golf ist endlich aus der Werkstatt zurück."),
        Message("S16-parents", "g1", "alex", _ms(NOW - timedelta(days=60)),
                "Meine Eltern sind nach Fulda gezogen."),
    ]
    return _scenario(
        "S16", "cross_chat", rng, subject="alex", trigger_chat="g3", facts=facts,
        needed=("S16-home", "S16-car", "S16-parents"),
        trigger_text="Ich fahr am Wochenende zu meinen Eltern.", asker="alex",
    )


def generate_retrieval_set(*, seed: int = 7, per_category: int = 6) -> list[Scenario]:
    rng = random.Random(seed)
    out: list[Scenario] = []
    for category in RETRIEVAL_CATEGORIES:
        for index in range(per_category):
            sid = f"R-{category}-{index}"
            subject = rng.choice(("frank", "alex", "lisa", "mara"))
            name = subject.capitalize()
            asker = rng.choice([person for person in ("kevin", "lisa", "mara", "alex") if person != subject])
            trigger_chat = "g1"
            city, city2 = rng.sample(CITIES, 2)
            if category == "single_recent":
                fact = Message(
                    f"{sid}-f", "g1", subject, _ms(NOW - timedelta(minutes=30)),
                    f"Ich wohne jetzt in {city}.",
                )
                out.append(_scenario(
                    sid, category, rng, subject=subject, trigger_chat=trigger_chat, facts=[fact],
                    needed=(fact.id,), asker=asker,
                    trigger_text=f"Wer fährt Samstag mit nach {city}?",
                ))
            elif category == "single_old":
                fact = Message(
                    f"{sid}-f", "g1", subject, _ms(NOW - timedelta(days=3)),
                    f"Ich wohne jetzt in {city}.",
                )
                out.append(_scenario(
                    sid, category, rng, subject=subject, trigger_chat=trigger_chat, facts=[fact],
                    needed=(fact.id,), asker=asker,
                    trigger_text=f"{name}, wie ist es eigentlich so in deiner neuen Stadt?",
                ))
            elif category == "cross_chat":
                company = rng.choice(COMPANIES)
                fact = Message(
                    f"{sid}-f", "g3", subject, _ms(NOW - timedelta(days=5)),
                    f"Ab Montag arbeite ich bei {company}.",
                )
                out.append(_scenario(
                    sid, category, rng, subject=subject, trigger_chat=trigger_chat, facts=[fact],
                    needed=(fact.id,), asker=asker,
                    trigger_text=f"{name}, wie läuft's im neuen Job?",
                ))
            elif category == "temporal":
                exam = rng.choice(SUBJECTS)
                fact = Message(
                    f"{sid}-f", "g1", subject, _ms(NOW - timedelta(days=6)),
                    f"Am Freitag habe ich meine Prüfung in {exam}.",
                )
                out.append(_scenario(
                    sid, category, rng, subject=subject, trigger_chat=trigger_chat, facts=[fact],
                    needed=(fact.id,), asker=asker,
                    trigger_text=f"{name}, wie lief eigentlich die Prüfung?",
                ))
            elif category == "update":
                old = Message(
                    f"{sid}-old", "g1", subject, _ms(NOW - timedelta(days=200)),
                    f"Ich wohne in {city}.",
                )
                new = Message(
                    f"{sid}-new", "g1", subject, _ms(NOW - timedelta(days=20)),
                    f"Wir sind nach {city2} umgezogen!",
                )
                out.append(_scenario(
                    sid, category, rng, subject=subject, trigger_chat=trigger_chat,
                    facts=[old, new], needed=(new.id,), asker=asker,
                    trigger_text=f"Wo wohnst du jetzt eigentlich, {name}?",
                ))
            else:
                out.append(_scenario(
                    sid, category, rng, subject=subject, trigger_chat=trigger_chat, facts=[],
                    needed=(), asker=asker, abstain=True,
                    trigger_text=f"Weiß jemand, wo {name} arbeitet?",
                ))
    out.append(_s16())
    return out


@dataclass(frozen=True, slots=True)
class JudgeView:
    evidence_ids: frozenset[str]
    memory_items: int
    tokens: int
    build_ms: float
    abstained: bool | None = None


async def today_participation_view(scenario: Scenario) -> JudgeView:
    _, context, build_ms = await build_today_judge_context(scenario)
    messages = cast(list[dict[str, object]], context.get("messages", []))
    ids = frozenset(str(message["event_id"]) for message in messages if message.get("event_id"))
    tokens = len(json.dumps(context, ensure_ascii=False, default=str)) // 4
    return JudgeView(ids, 0, tokens, build_ms)


@dataclass(frozen=True, slots=True)
class RetrievalSummary:
    by_category: dict[str, dict[str, float]]
    overall: dict[str, float]
    scenarios: list[dict[str, object]]


def _metrics(rows: list[dict[str, object]]) -> dict[str, float]:
    with_needed = [row for row in rows if row["needed"]]
    recalls = [float(row["recall"]) for row in with_needed]  # type: ignore[arg-type]
    abstain_rows = [row for row in rows if row["expect_abstain"]]
    measurable = [row for row in abstain_rows if row["abstained"] is not None]
    builds = sorted(float(row["build_ms"]) for row in rows)  # type: ignore[arg-type]
    return {
        "scenarios": float(len(rows)),
        "needed_recall": round(statistics.fmean(recalls), 4) if recalls else -1.0,
        "full_recall_rate": round(sum(recall == 1.0 for recall in recalls) / len(recalls), 4)
        if recalls else -1.0,
        "abstention_correct": (
            round(sum(bool(row["abstained"]) for row in measurable) / len(measurable), 4)
            if measurable else -1.0
        ),
        "memory_items_mean": round(
            statistics.fmean(float(row["memory_items"]) for row in rows), 2,
        ),
        "tokens_mean": round(statistics.fmean(float(row["tokens"]) for row in rows), 1),
        "build_ms_p50": round(builds[len(builds) // 2], 2),
        "build_ms_p95": round(builds[min(len(builds) - 1, int(len(builds) * 0.95))], 2),
    }


async def score_retrieval(
    scenarios: list[Scenario], provider: Callable[[Scenario], Awaitable[JudgeView]],
) -> RetrievalSummary:
    rows: list[dict[str, object]] = []
    for scenario in scenarios:
        view = await provider(scenario)
        needed = set(scenario.expected.needed_evidence)
        found = needed & set(view.evidence_ids)
        rows.append({
            "id": scenario.id,
            "category": scenario.category,
            "needed": sorted(needed),
            "found": sorted(found),
            "recall": (len(found) / len(needed)) if needed else 1.0,
            "expect_abstain": scenario.expected.abstain,
            "abstained": view.abstained,
            "memory_items": view.memory_items,
            "tokens": view.tokens,
            "build_ms": view.build_ms,
        })
    categories = sorted({str(row["category"]) for row in rows})
    by_category = {
        category: _metrics([row for row in rows if row["category"] == category])
        for category in categories
    }
    return RetrievalSummary(by_category, _metrics(rows), rows)
