"""Deterministic severity prefixes for overseer alerts."""
from __future__ import annotations

_PREFIXES = ("🟢 INFO ", "🟡 ACTION ", "🔴 CRITICAL ")

_CRITICAL_TERMS = (
    "critical",
    "gateway down",
    "bridge down",
    "service down",
    "disk usage above",
    "repeated failure",
    "repeated restart",
)

_ACTION_TERMS = (
    "aborted",
    "deletion skipped",
    "could not run",
    "failed",
    "missing from policy",
    "not present in policy",
    "budget_exhausted",
    "budget exhausted",
)

_INFO_TERMS = (
    "no action",
    "nothing matched",
    "0 rows",
    "0.",
    "none missing",
    "no deletion",
    "prune not executed",
    "checks completed",
    "success",
)


def classify_overseer_alert(message: str) -> str:
    """Return INFO, ACTION, or CRITICAL for an overseer alert message."""
    text = message.lower()
    if any(term in text for term in _CRITICAL_TERMS):
        return "CRITICAL"
    if any(term in text for term in _ACTION_TERMS):
        if "none missing" in text or "missing from policy.json: none" in text:
            return "INFO"
        return "ACTION"
    if any(term in text for term in _INFO_TERMS):
        return "INFO"
    return "INFO"


def format_overseer_alert(message: str) -> str:
    """Prefix an overseer alert with a stable severity marker."""
    if message.startswith(_PREFIXES):
        return message

    severity = classify_overseer_alert(message)
    prefix = {
        "INFO": "🟢 INFO ",
        "ACTION": "🟡 ACTION ",
        "CRITICAL": "🔴 CRITICAL ",
    }[severity]
    return f"{prefix}{message}"
