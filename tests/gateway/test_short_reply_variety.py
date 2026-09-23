from __future__ import annotations

from yeoman_gateway.short_reply.variety import choose_varied, cooldown_active


def test_without_history_the_best_fit_wins() -> None:
    assert choose_varied(["😎", "😄", "🤙"], []) == "😎"


def test_a_recently_used_best_fit_yields_to_the_next_fitting_one() -> None:
    assert choose_varied(["😎", "😄", "🤙"], ["😎", "👍"]) == "😄"


def test_when_every_candidate_was_used_the_longest_unused_one_wins() -> None:
    # newest first: 😄 just now, 😎 before, 🤙 longest ago
    assert choose_varied(["😎", "😄", "🤙"], ["😄", "😎", "🤙"]) == "🤙"


def test_variety_never_invents_an_emoji() -> None:
    assert choose_varied(["😂"], ["😂", "😂"]) is None
    assert choose_varied([], ["👍"]) is None


def test_a_safe_alternative_beats_a_candidate_that_would_repeat_three_times() -> None:
    assert choose_varied(["😂", "💀"], ["😂", "😂"]) == "💀"


def test_duplicates_and_blanks_in_candidates_are_ignored() -> None:
    assert choose_varied(["", "👍", "👍", "🔥"], ["👍"]) == "🔥"


def test_three_decisions_in_a_row_do_not_repeat_when_alternatives_fit() -> None:
    recent: list[str] = []
    chosen = []
    for _ in range(3):
        emoji = choose_varied(["😂", "💀", "😄"], recent)
        assert emoji is not None
        chosen.append(emoji)
        recent.insert(0, emoji)
    assert chosen == ["😂", "💀", "😄"]


def test_cooldown_starts_after_a_burst_and_ends_after_its_duration() -> None:
    burst = [100_000, 20_000]  # two reactions 80 s apart, newest first
    assert cooldown_active(burst, now_ms=150_000, count=2, window_seconds=120, cooldown_seconds=600)
    assert not cooldown_active(
        burst, now_ms=100_000 + 600_000, count=2, window_seconds=120, cooldown_seconds=600
    )


def test_spread_out_reactions_do_not_trigger_the_cooldown() -> None:
    spread = [500_000, 100_000]  # 400 s apart
    assert not cooldown_active(
        spread, now_ms=510_000, count=2, window_seconds=120, cooldown_seconds=600
    )


def test_too_few_reactions_never_trigger_the_cooldown() -> None:
    assert not cooldown_active(
        [100_000], now_ms=100_001, count=2, window_seconds=120, cooldown_seconds=600
    )
