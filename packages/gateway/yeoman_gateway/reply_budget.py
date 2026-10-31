"""Reply-budget helpers for compact WhatsApp answers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

LongFormBypassMode = Literal["off", "owner_only", "always"]

DEFAULT_REPLY_BUDGET_TARGETS: dict[str, int] = {
    "social_one_liner": 180,
    "one_liner": 220,
    "short_take": 420,
    "repair": 700,
    "researched_answer": 1200,
    #: A turn that only acknowledges handed-off or already running work. Its answer is a
    #: status line, never the work itself, so it is budgeted far below a short take.
    "ack": 180,
}
DEFAULT_REPLY_BUDGET_HARD_MAX_CHARS = 600
#: Shapes the runtime chose structurally (an acknowledgement, a social beat, a one-line
#: follow-up): their target is also their ceiling, so a longer draft is rewritten shorter.
_TARGET_IS_CEILING: frozenset[str] = frozenset({"ack", "social_one_liner", "one_liner"})
DEFAULT_REPLY_BUDGET_LONG_FORM_MAX_CHARS = 2200
#: Tools whose results are evidence the answer has to carry. A turn that called one is
#: budgeted like a researched answer, whatever shape the request was filed under.
RESEARCH_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "web_search",
        "web_fetch",
        "web_map",
        "web_crawl",
        "deep_research",
        "fact_check",
        "browse",
        "youtube_transcript",
        "market_quote",
        "market_intelligence",
    }
)

@dataclass(frozen=True, slots=True)
class ReplyBudgetDecision:
    enabled: bool
    answer_shape: str
    target_chars: int
    hard_max_chars: int
    long_form_max_chars: int
    long_form_allowed: bool
    hard_cap_enabled: bool
    session_history_limit: int | None = None
    instruction: str = ""

    def as_metadata(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "answer_shape": self.answer_shape,
            "target_chars": self.target_chars,
            "hard_max_chars": self.hard_max_chars,
            "long_form_max_chars": self.long_form_max_chars,
            "long_form_allowed": self.long_form_allowed,
            "hard_cap_enabled": self.hard_cap_enabled,
            "session_history_limit": self.session_history_limit,
            "instruction": self.instruction,
        }


def coerce_reply_budget_policy(raw: object) -> dict[str, object]:
    """Normalize a possibly-partial policy dict for runtime use."""
    if not isinstance(raw, dict):
        raw = {}
    targets_raw = raw.get("targets")
    targets = dict(DEFAULT_REPLY_BUDGET_TARGETS)
    if isinstance(targets_raw, dict):
        for key, value in targets_raw.items():
            name = str(key or "").strip()
            if not name:
                continue
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                targets[name] = parsed

    def positive_int(key: str, default: int) -> int:
        try:
            parsed = int(raw.get(key) or default)
        except (TypeError, ValueError):
            return default
        return max(1, parsed)

    def optional_positive_int(key: str) -> int | None:
        value = raw.get(key)
        if value is None:
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return max(1, parsed)

    long_form_bypass = str(raw.get("long_form_bypass") or "owner_only").strip()
    if long_form_bypass not in {"off", "owner_only", "always"}:
        long_form_bypass = "owner_only"

    return {
        "enabled": bool(raw.get("enabled", False)),
        "targets": targets,
        "hard_max_chars": positive_int("hard_max_chars", DEFAULT_REPLY_BUDGET_HARD_MAX_CHARS),
        "long_form_max_chars": positive_int(
            "long_form_max_chars",
            DEFAULT_REPLY_BUDGET_LONG_FORM_MAX_CHARS,
        ),
        "long_form_bypass": long_form_bypass,
        "session_history_limit": optional_positive_int("session_history_limit"),
    }


def derive_reply_budget(
    *,
    policy: object,
    answer_shape: str,
    is_owner: bool,
) -> ReplyBudgetDecision | None:
    cfg = coerce_reply_budget_policy(policy)
    if not bool(cfg["enabled"]):
        return None

    shape = str(answer_shape or "short_take").strip() or "short_take"
    targets = cfg["targets"]
    assert isinstance(targets, dict)
    target_chars = int(
        str(
            targets.get(shape)
            or targets.get("short_take")
            or DEFAULT_REPLY_BUDGET_HARD_MAX_CHARS
        )
    )
    hard_max_chars = int(str(cfg["hard_max_chars"]))
    if shape in _TARGET_IS_CEILING:
        hard_max_chars = min(hard_max_chars, target_chars)
    long_form_max_chars = int(str(cfg["long_form_max_chars"]))
    long_form_bypass = str(cfg["long_form_bypass"])
    # Who may get long answers is policy; whether this answer needs the room is the
    # writing model's judgment, made while it writes - no keyword guess, no extra call.
    long_form_allowed = shape not in _TARGET_IS_CEILING and (
        long_form_bypass == "always" or (long_form_bypass == "owner_only" and is_owner)
    )
    hard_cap_enabled = not long_form_allowed
    ceiling = long_form_max_chars if long_form_allowed else hard_max_chars

    instruction = (
        "Use the smallest complete answer that satisfies the user. "
        f"Aim for about {target_chars} characters. Go longer only when the answer "
        "needs it: the person asked for detail or an explanation, or a required fact, "
        "number, caveat, source, link or code would otherwise be lost. "
        f"Even then stay under {ceiling} characters; when you report findings from "
        f"research or market tools, stay under {long_form_max_chars}."
    )

    session_history_limit = cfg["session_history_limit"]
    assert session_history_limit is None or isinstance(session_history_limit, int)
    return ReplyBudgetDecision(
        enabled=True,
        answer_shape=shape,
        target_chars=target_chars,
        hard_max_chars=hard_max_chars,
        long_form_max_chars=long_form_max_chars,
        long_form_allowed=long_form_allowed,
        hard_cap_enabled=hard_cap_enabled,
        session_history_limit=session_history_limit,
        instruction=instruction,
    )


def enforce_reply_budget(
    text: str,
    budget: object,
    *,
    research_used: bool = False,
) -> tuple[str, dict[str, object]]:
    """Measure a reply against its budget's ceiling; the text itself is never cut.

    The target is guidance the writing model weighs; the ceiling is the backstop. A
    reply over it comes back unchanged with ``over_budget`` and the ``limit`` it missed,
    so the caller can ask the model for a shorter version. A sentence sliced out of a
    written answer reads as a broken answer; the full one does not.
    """
    original = str(text or "").strip()
    if not original or not isinstance(budget, dict) or not bool(budget.get("enabled", False)):
        return original, {"over_budget": False, "reason": "disabled"}

    fixed_shape = budget.get("answer_shape") in _TARGET_IS_CEILING
    if (research_used and not fixed_shape) or bool(budget.get("long_form_allowed", False)):
        limit = int(budget.get("long_form_max_chars") or DEFAULT_REPLY_BUDGET_LONG_FORM_MAX_CHARS)
        reason = "long_form_max_chars"
    else:
        limit = int(budget.get("hard_max_chars") or DEFAULT_REPLY_BUDGET_HARD_MAX_CHARS)
        reason = "hard_max_chars"
    limit = max(1, limit)
    if len(original) <= limit:
        return original, {"over_budget": False, "reason": "within_budget", "limit": limit}
    return original, {
        "over_budget": True,
        "reason": reason,
        "limit": limit,
        "before_chars": len(original),
    }


def shorten_reply_messages(text: str, limit: int) -> list[dict[str, str]]:
    """Prompt asking the model to rewrite its own reply within ``limit`` characters."""
    return [
        {
            "role": "system",
            "content": (
                "You shorten a chat reply that is already written. Rewrite it in at most "
                f"{limit} characters. Keep its language, voice and point. Keep every number, "
                "name, source and link the answer depends on; drop filler and repetition "
                "instead. Do not add anything new. Output only the rewritten reply."
            ),
        },
        {"role": "user", "content": text},
    ]


def accept_shortened_reply(original: str, rewrite: str | None, limit: int) -> str | None:
    """Return the rewrite when it really fits the budget, otherwise None."""
    candidate = str(rewrite or "").strip()
    if not candidate or len(candidate) > limit or len(candidate) >= len(original):
        return None
    return candidate
