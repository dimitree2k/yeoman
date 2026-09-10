"""Plan 06 / Aufgabe 1: send budgets slide, are attempt-idempotent and survive restarts."""

from __future__ import annotations

import threading
from pathlib import Path

from yeoman_gateway.processing.budget import (
    CHAT_HARD_UNITS,
    REACTION_TTL_MS,
    REPLY_TTL_MS,
    ChatBudget,
    ThreadBudget,
    available_units,
    effect_ttl_ms,
    next_ready_ms,
    within_deadline,
)
from yeoman_gateway.processing.store import ProcessingStore

CHANNEL = "whatsapp"
CHAT = "chat-1"


def _store(tmp_path: Path) -> ProcessingStore:
    return ProcessingStore(tmp_path / "processing.db")


def test_budget_does_not_reset_at_minute_boundary() -> None:
    assert available_units([(59_999, 6)], 60_001) == 0
    assert available_units([(59_999, 6)], 119_999) == 6


def test_window_ignores_attempts_older_than_the_window() -> None:
    attempts = [(0, 6), (30_000, 3)]

    # At t=30s the first attempt is still inside the 60s window, so both count.
    assert available_units(attempts, 30_001, limit=6, window_ms=60_000) == 0
    assert available_units(attempts, 60_001, limit=6, window_ms=60_000) == 3
    assert available_units(attempts, 90_001, limit=6, window_ms=60_000) == 6


def test_next_ready_is_deterministic() -> None:
    assert next_ready_ms([(1_000, 6)], 2_000) == 61_000
    # The limit is crossed at the *second* attempt, so capacity returns 60s after it.
    assert next_ready_ms([(1_000, 3), (2_000, 3)], 2_500) == 62_000
    assert next_ready_ms([], 2_500) == 2_500


def test_ttls_match_the_capability() -> None:
    assert effect_ttl_ms("reaction") == REACTION_TTL_MS == 30_000
    assert effect_ttl_ms("send_text") == REPLY_TTL_MS == 120_000
    assert within_deadline(queued_ms=0, now_ms=29_999, ttl_ms=REACTION_TTL_MS)
    assert not within_deadline(queued_ms=0, now_ms=30_000, ttl_ms=REACTION_TTL_MS)


def test_reservation_is_limited_and_attempt_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    budget = ChatBudget(store)

    for index in range(CHAT_HARD_UNITS):
        decision = budget.reserve(
            channel=CHANNEL, chat_id=CHAT, effect_id=f"fx{index}",
            attempt_id=f"a{index}", now_ms=1_000,
        )
        assert decision.allowed

    refused = budget.reserve(
        channel=CHANNEL, chat_id=CHAT, effect_id="fx9", attempt_id="a9", now_ms=1_000
    )
    assert not refused.allowed
    assert refused.reason == "budget_exhausted"
    assert refused.ready_at_ms == 61_000

    # The very same attempt is free to retry its reservation.
    assert budget.reserve(
        channel=CHANNEL, chat_id=CHAT, effect_id="fx0", attempt_id="a0", now_ms=1_000
    ).allowed
    assert store.count_send_budget_reservations() == CHAT_HARD_UNITS
    store.close()


def test_capacity_returns_after_the_window(tmp_path: Path) -> None:
    store = _store(tmp_path)
    budget = ChatBudget(store)
    for index in range(CHAT_HARD_UNITS):
        budget.reserve(
            channel=CHANNEL, chat_id=CHAT, effect_id=f"fx{index}",
            attempt_id=f"a{index}", now_ms=1_000,
        )

    assert not budget.reserve(
        channel=CHANNEL, chat_id=CHAT, effect_id="fx9", attempt_id="a9", now_ms=60_000
    ).allowed
    assert budget.reserve(
        channel=CHANNEL, chat_id=CHAT, effect_id="fx9", attempt_id="a9", now_ms=61_001
    ).allowed
    store.close()


def test_budget_is_per_chat(tmp_path: Path) -> None:
    store = _store(tmp_path)
    budget = ChatBudget(store)
    for index in range(CHAT_HARD_UNITS):
        budget.reserve(
            channel=CHANNEL, chat_id=CHAT, effect_id=f"fx{index}",
            attempt_id=f"a{index}", now_ms=1_000,
        )

    assert budget.available(channel=CHANNEL, chat_id="chat-2", now_ms=1_000) == CHAT_HARD_UNITS
    assert budget.reserve(
        channel=CHANNEL, chat_id="chat-2", effect_id="fx", attempt_id="b1", now_ms=1_000
    ).allowed
    store.close()


def test_media_units_count_individually(tmp_path: Path) -> None:
    store = _store(tmp_path)
    budget = ChatBudget(store)

    assert budget.reserve(
        channel=CHANNEL, chat_id=CHAT, effect_id="fx", attempt_id="a1", units=4, now_ms=1_000
    ).allowed
    assert budget.available(channel=CHANNEL, chat_id=CHAT, now_ms=1_000) == 2
    assert not budget.reserve(
        channel=CHANNEL, chat_id=CHAT, effect_id="fx2", attempt_id="a2", units=3, now_ms=1_000
    ).allowed
    store.close()


def test_spend_survives_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "processing.db"
    store = ProcessingStore(path)
    budget = ChatBudget(store)
    for index in range(CHAT_HARD_UNITS):
        budget.reserve(
            channel=CHANNEL, chat_id=CHAT, effect_id=f"fx{index}",
            attempt_id=f"a{index}", now_ms=1_000,
        )
    store.close()

    reopened = ProcessingStore(path)
    fresh_budget = ChatBudget(reopened)

    assert fresh_budget.available(channel=CHANNEL, chat_id=CHAT, now_ms=1_500) == 0
    assert not fresh_budget.reserve(
        channel=CHANNEL, chat_id=CHAT, effect_id="fx9", attempt_id="a9", now_ms=1_500
    ).allowed
    reopened.close()


def test_concurrent_dispatchers_never_exceed_the_limit(tmp_path: Path) -> None:
    store = _store(tmp_path)
    budget = ChatBudget(store)
    granted: list[int] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def dispatch(index: int) -> None:
        barrier.wait()
        decision = budget.reserve(
            channel=CHANNEL, chat_id=CHAT, effect_id=f"fx{index}",
            attempt_id=f"a{index}", now_ms=5_000,
        )
        if decision.allowed:
            with lock:
                granted.append(index)

    threads = [threading.Thread(target=dispatch, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(granted) == CHAT_HARD_UNITS
    assert store.count_send_budget_reservations() == CHAT_HARD_UNITS
    store.close()


def test_thread_budget_defers_and_does_not_forbid() -> None:
    budget = ThreadBudget(limit=2, window_ms=10_000)

    assert budget.defer_until(thread_id="th1", now_ms=0) == 0
    budget.note_send(thread_id="th1", now_ms=0)
    budget.note_send(thread_id="th1", now_ms=1_000)

    assert budget.defer_until(thread_id="th1", now_ms=1_500) == 10_000
    assert budget.defer_until(thread_id="th1", now_ms=10_001) == 10_001
    assert budget.defer_until(thread_id="th2", now_ms=1_500) == 1_500


def test_round_robin_prefers_ready_threads_then_the_oldest_wait() -> None:
    budget = ThreadBudget(limit=1, window_ms=10_000)
    budget.note_send(thread_id="th-old", now_ms=0)
    budget.note_send(thread_id="th-new", now_ms=5_000)

    order = budget.round_robin(["th-new", "th-free", "th-old"], now_ms=6_000)

    assert order[0] == "th-free"
    assert order[1] == "th-old"
    assert order[2] == "th-new"
