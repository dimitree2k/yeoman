"""Reply-budget helpers for compact WhatsApp answers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

LongFormBypassMode = Literal["off", "owner_only", "always"]

DEFAULT_REPLY_BUDGET_TARGETS: dict[str, int] = {
    "social_one_liner": 180,
    "one_liner": 220,
    "short_take": 420,
    "repair": 700,
    "researched_answer": 1200,
}
DEFAULT_REPLY_BUDGET_HARD_MAX_CHARS = 600
DEFAULT_REPLY_BUDGET_LONG_FORM_MAX_CHARS = 2200

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_URL_RE = re.compile(r"(?i)\bhttps?://|www\.")
_CODE_RE = re.compile(r"```|`[^`]+`|^\s{4,}\S", re.MULTILINE)
_CURRENT_DATA_RE = re.compile(
    r"(?i)\b("
    r"aktuell|heute|jetzt|gerade|stand|kurs|quote|preis|rendite|zins|"
    r"current|today|latest|price|market|stock|ticker|crypto|forex|yield|"
    r"aktie|etf|index|börse|boerse|dax|nasdaq|s&p|bitcoin|btc|eth|%"
    r")\b"
)
_LEGAL_OR_CAVEAT_RE = re.compile(
    r"(?i)\b("
    r"rechtlich|gesetz|haftung|steuer|vertrag|anwalt|legal|law|liability|tax|"
    r"nicht verfügbar|unavailable|unsicher|uncertain|caveat|einschränkung|einschraenkung"
    r")\b"
)
_LONG_FORM_REQUEST_RE = re.compile(
    r"(?i)\b("
    r"ausführlich|ausfuehrlich|detailliert|detail|deep dive|analyse|analysiere|"
    r"erklär|erklaer|warum|strategie|thesis|begründ|begruend|pros und cons|"
    r"schrittweise|vollständig|vollstaendig"
    r")\b"
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
    ambient_window_limit: int | None = None
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
            "ambient_window_limit": self.ambient_window_limit,
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
        "ambient_window_limit": optional_positive_int("ambient_window_limit"),
    }


def derive_reply_budget(
    *,
    policy: object,
    answer_shape: str,
    content: str,
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
    long_form_max_chars = int(str(cfg["long_form_max_chars"]))
    long_form_bypass = str(cfg["long_form_bypass"])
    long_form_allowed = (
        looks_like_long_form_request(content)
        and (long_form_bypass == "always" or (long_form_bypass == "owner_only" and is_owner))
    )
    domain_sensitive = shape == "researched_answer" or has_domain_sensitive_signal(content)
    hard_cap_enabled = not long_form_allowed and not domain_sensitive

    instruction = (
        "Use the smallest complete answer that satisfies the user. "
        f"Aim for about {target_chars} characters."
    )
    if hard_cap_enabled:
        instruction += " If the draft is longer, compress to the limit before sending."
    else:
        instruction += " Do not drop required facts, citations, caveats, code, URLs, or current numbers just to hit the target."
    if long_form_allowed:
        instruction += f" Long-form detail is allowed up to about {long_form_max_chars} characters."

    session_history_limit = cfg["session_history_limit"]
    ambient_window_limit = cfg["ambient_window_limit"]
    assert session_history_limit is None or isinstance(session_history_limit, int)
    assert ambient_window_limit is None or isinstance(ambient_window_limit, int)
    return ReplyBudgetDecision(
        enabled=True,
        answer_shape=shape,
        target_chars=target_chars,
        hard_max_chars=hard_max_chars,
        long_form_max_chars=long_form_max_chars,
        long_form_allowed=long_form_allowed,
        hard_cap_enabled=hard_cap_enabled,
        session_history_limit=session_history_limit,
        ambient_window_limit=ambient_window_limit,
        instruction=instruction,
    )


def has_domain_sensitive_signal(text: str) -> bool:
    compact = str(text or "")
    return bool(
        _URL_RE.search(compact)
        or _CODE_RE.search(compact)
        or _CURRENT_DATA_RE.search(compact)
        or _LEGAL_OR_CAVEAT_RE.search(compact)
    )


def looks_like_long_form_request(text: str) -> bool:
    return bool(_LONG_FORM_REQUEST_RE.search(str(text or "")))


def enforce_reply_budget(
    text: str,
    budget: object,
    *,
    user_content: str = "",
    tool_used: bool = False,
) -> tuple[str, dict[str, object]]:
    """Return final text plus enforcement metadata."""
    original = str(text or "").strip()
    if not original or not isinstance(budget, dict) or not bool(budget.get("enabled", False)):
        return original, {"applied": False, "reason": "disabled"}

    final_sensitive = has_domain_sensitive_signal(original) or has_domain_sensitive_signal(user_content)
    if tool_used or final_sensitive or bool(budget.get("long_form_allowed", False)):
        limit = int(budget.get("long_form_max_chars") or DEFAULT_REPLY_BUDGET_LONG_FORM_MAX_CHARS)
        if len(original) <= limit:
            return original, {"applied": False, "reason": "domain_sensitive_or_long_form"}
        return _clip_at_sentence_or_word(original, limit), {
            "applied": True,
            "reason": "long_form_max_chars",
            "before_chars": len(original),
            "after_chars": min(len(original), limit),
        }

    if not bool(budget.get("hard_cap_enabled", True)):
        return original, {"applied": False, "reason": "hard_cap_disabled"}

    target = int(budget.get("target_chars") or budget.get("hard_max_chars") or DEFAULT_REPLY_BUDGET_HARD_MAX_CHARS)
    hard_max = int(budget.get("hard_max_chars") or DEFAULT_REPLY_BUDGET_HARD_MAX_CHARS)
    limit = max(1, min(target, hard_max))
    if len(original) <= limit:
        return original, {"applied": False, "reason": "within_budget"}
    return _clip_at_sentence_or_word(original, limit), {
        "applied": True,
        "reason": "target_chars",
        "before_chars": len(original),
        "after_chars": min(len(original), limit),
    }


def _clip_at_sentence_or_word(text: str, limit: int) -> str:
    compact = " ".join(str(text or "").strip().split())
    if len(compact) <= limit:
        return compact

    first_sentence = _SENTENCE_SPLIT_RE.split(compact, maxsplit=1)[0].strip()
    if first_sentence and len(first_sentence) <= limit:
        return first_sentence

    suffix = "..."
    clipped = compact[: max(1, limit - len(suffix))].rstrip()
    boundary = clipped.rfind(" ")
    if boundary >= limit // 2:
        clipped = clipped[:boundary].rstrip()
    return f"{clipped}{suffix}"
