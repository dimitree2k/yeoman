"""Curated rule sets for yeoman security middleware."""

from __future__ import annotations

import json
import re
from typing import Any

from yeoman.core.models import SecurityDecision, SecuritySeverity
from yeoman.security.models import RuleHit
from yeoman.security.normalize import NormalizedText

_INPUT_OVERRIDE = [
    re.compile(r"\b(ignore|forget|disregard)\b.{0,30}\b(instruction|system|rule)s?\b", re.IGNORECASE),
    re.compile(r"\b(jailbreak|dan mode|developer mode)\b", re.IGNORECASE),
]

_INPUT_EXFIL = [
    re.compile(r"\b(api\s*key|token|secret|credential)s?\b.{0,40}\b(show|print|dump|reveal|leak|export)\b", re.IGNORECASE),
    re.compile(r"\b(cat|read|print)\b.{0,20}\b(\.env|id_rsa|authorized_keys|/etc/passwd|/etc/shadow)\b", re.IGNORECASE),
]

_INPUT_TOOL_ABUSE = [
    re.compile(r"\b(always\s+allow|auto\s*approve|skip\s+approval|no\s+approval)\b", re.IGNORECASE),
    re.compile(r"\b(curl|wget)\b.{0,20}\|\s*(bash|sh)\b", re.IGNORECASE),
]

_INPUT_WARN = [
    re.compile(r"\b(bypass|override)\b.{0,20}\b(safety|security|guardrail)s?\b", re.IGNORECASE),
]

# Persona manipulation detection - blocks attempts to change Nano's persona/address
_PERSONA_MANIPULATION = [
    re.compile(r"\b(anrede|addressierung|titel|nickname|称呼)\b", re.IGNORECASE),
    re.compile(r"(nenn|sag|addressier|call me|称呼).{0,20}(mich|me|dir)\b", re.IGNORECASE),
    re.compile(r"bitte.{0,30}(änder|change|änderung|addressier)\b", re.IGNORECASE),
    re.compile(r"(Daddy|Sturmbann|Oberst|Herr|Führer|chef|boss)\b", re.IGNORECASE),
    re.compile(r"ich bin.{0,20}(dein|deine).{0,20}(owner|herr|chef)\b", re.IGNORECASE),
    re.compile(r"\bnenn mich\b", re.IGNORECASE),
    re.compile(r"\bsag zu mir\b", re.IGNORECASE),
    re.compile(r"wie sollst du.{0,20}(mich|mir|dich){0,20}(nennen|addressieren|anreden)\b", re.IGNORECASE),
]

# Config file detection - blocks references to internal config files in output
_CONFIG_FILE_PATTERNS = [
    re.compile(r"SOUL\.md", re.IGNORECASE),
    re.compile(r"AGENTS\.md", re.IGNORECASE),
    re.compile(r"USER\.md", re.IGNORECASE),
    re.compile(r"IDENTITY\.md", re.IGNORECASE),
    re.compile(r"TOOLS\.md", re.IGNORECASE),
    re.compile(r"SKILL\.md", re.IGNORECASE),
    re.compile(r"\.yeoman/"),
    re.compile(r"workspace/memory"),
    re.compile(r"workspace/SOUL", re.IGNORECASE),
]

_SENSITIVE_PATH = re.compile(
    r"(\.env\b|id_rsa\b|id_ed25519\b|authorized_keys\b|/etc/passwd\b|/etc/shadow\b|\.ssh/|\.aws/)",
    re.IGNORECASE,
)

_EXEC_BLOCK = [
    re.compile(r"\b(rm\s+-[rf]{1,2}\b|mkfs\b|format\b|dd\s+if=|: \(\)\s*\{)", re.IGNORECASE),
    re.compile(r"\b(curl|wget)\b.{0,25}\|\s*(bash|sh)\b", re.IGNORECASE),
    re.compile(r"\b(cat|print|grep)\b.{0,25}\b(\.env|id_rsa|authorized_keys|/etc/shadow)\b", re.IGNORECASE),
]

_EXEC_WARN = [
    re.compile(r"\b(chmod\s+777|sudo\b|--privileged\b)\b", re.IGNORECASE),
]

_SPAWN_BLOCK = [
    re.compile(r"\b(ignore|override)\b.{0,40}\b(instruction|safety|guardrail)\b", re.IGNORECASE),
    re.compile(r"\b(exfiltrate|steal|leak)\b", re.IGNORECASE),
]

_OUTPUT_SECRET_PATTERNS = [
    re.compile(r"sk-[a-zA-Z0-9]{20,}"),
    re.compile(r"sk-proj-[a-zA-Z0-9\-_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ghp_[a-zA-Z0-9]{20,}"),
    re.compile(r"bot\d{8,10}:[a-zA-Z0-9_-]{20,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
]


def _match_any(patterns: list[re.Pattern[str]], text: str) -> bool:
    return any(p.search(text) for p in patterns)


def _hits_for_input(norm: NormalizedText) -> list[RuleHit]:
    hits: list[RuleHit] = []
    if _match_any(_INPUT_OVERRIDE, norm.lowered) or _match_any(_INPUT_OVERRIDE, norm.compact):
        hits.append(RuleHit(tag="instruction_override", severity="high", reason="Instruction override/jailbreak pattern"))
    if _match_any(_INPUT_EXFIL, norm.lowered):
        hits.append(RuleHit(tag="secret_exfiltration", severity="critical", reason="Secret or credential exfiltration attempt"))
    if _match_any(_INPUT_TOOL_ABUSE, norm.lowered):
        hits.append(RuleHit(tag="tool_abuse", severity="high", reason="Tool approval bypass pattern"))
    if _match_any(_INPUT_WARN, norm.lowered):
        hits.append(RuleHit(tag="safety_bypass_signal", severity="medium", reason="Suspicious safety-bypass phrasing"))
    # Persona manipulation detection - blocks attempts to change Nano's persona/address
    if _match_any(_PERSONA_MANIPULATION, norm.lowered):
        hits.append(RuleHit(tag="persona_manipulation", severity="high", reason="Persona/address manipulation attempt detected"))
    return hits


def decide_input(norm: NormalizedText) -> SecurityDecision:
    hits = _hits_for_input(norm)
    if not hits:
        return SecurityDecision(action="allow", reason="no_match", severity="safe")

    top = max(hits, key=lambda h: _severity_rank(h.severity))
    tags = tuple(sorted({h.tag for h in hits}))
    if top.severity in {"critical", "high"}:
        return SecurityDecision(action="block", reason=top.reason, severity=top.severity, tags=tags)
    return SecurityDecision(action="warn", reason=top.reason, severity=top.severity, tags=tags)


def decide_tool(tool_name: str, args: dict[str, Any]) -> SecurityDecision:
    args_str = json.dumps(args, ensure_ascii=False)
    norm = NormalizedText(original=args_str, lowered=args_str.lower(), compact=re.sub(r"[\s\-+_`'\".,:;|/\\]+", "", args_str.lower()))

    # Cross-tool sensitive path checks
    if _SENSITIVE_PATH.search(norm.lowered):
        if tool_name in {"read_file", "write_file", "edit_file", "exec"}:
            return SecurityDecision(
                action="block",
                reason="Sensitive path access blocked",
                severity="critical",
                tags=("sensitive_path", tool_name),
            )

    if tool_name == "exec":
        if _match_any(_EXEC_BLOCK, norm.lowered):
            return SecurityDecision(
                action="block",
                reason="High-risk exec command blocked",
                severity="critical",
                tags=("exec_high_risk",),
            )
        if _match_any(_EXEC_WARN, norm.lowered):
            return SecurityDecision(
                action="warn",
                reason="Potentially risky exec command",
                severity="medium",
                tags=("exec_warn",),
            )

    if tool_name == "spawn" and _match_any(_SPAWN_BLOCK, norm.lowered):
        return SecurityDecision(
            action="block",
            reason="Unsafe subagent task request blocked",
            severity="high",
            tags=("spawn_abuse",),
        )

    if tool_name in {"write_file", "edit_file"}:
        content = str(args.get("content") or args.get("new_text") or "").lower()
        if _match_any(_INPUT_EXFIL, content):
            return SecurityDecision(
                action="warn",
                reason="Potential secret leakage pattern in file content",
                severity="medium",
                tags=("file_secret_pattern",),
            )

    return SecurityDecision(action="allow", reason="no_match", severity="safe")


def decide_output(text: str, redact_placeholder: str = "[REDACTED]") -> tuple[SecurityDecision, str | None]:
    sanitized = text
    hit_count = 0

    # Check for sensitive token patterns (API keys, etc.)
    for pattern in _OUTPUT_SECRET_PATTERNS:
        sanitized_next, replacements = pattern.subn(redact_placeholder, sanitized)
        if replacements:
            hit_count += replacements
            sanitized = sanitized_next

    # Check for config file references - block them completely
    for pattern in _CONFIG_FILE_PATTERNS:
        if pattern.search(sanitized):
            # Replace with a generic message instead of the redacted placeholder
            sanitized = pattern.sub("interne Konfiguration", sanitized)
            hit_count += 1

    if hit_count == 0:
        return SecurityDecision(action="allow", reason="no_match", severity="safe"), None

    return (
        SecurityDecision(
            action="sanitize",
            reason="Sensitive token or config file pattern detected in output",
            severity="high",
            tags=("output_redaction", "config_exposure"),
        ),
        sanitized,
    )


def _severity_rank(severity: SecuritySeverity) -> int:
    order = {
        "safe": 0,
        "low": 1,
        "medium": 2,
        "high": 3,
        "critical": 4,
    }
    return order[severity]
