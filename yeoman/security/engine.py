"""Nanobot-native security middleware engine."""

from __future__ import annotations

import re

from loguru import logger

from yeoman.config.schema import SecurityConfig
from yeoman.core.models import SecurityDecision, SecurityResult, SecurityStage
from yeoman.core.ports import SecurityPort
from yeoman.security.normalize import normalize_text
from yeoman.security.rules import decide_input, decide_output, decide_tool

_SENSITIVE_CONTEXT_KEYS = (
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "auth",
    "credential",
    "private_key",
    "cookie",
)

_SENSITIVE_VALUE_PATTERNS = (
    re.compile(r"sk-[a-zA-Z0-9]{20,}"),
    re.compile(r"sk-proj-[a-zA-Z0-9\-_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ghp_[a-zA-Z0-9]{20,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE),
    re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    ),
)

_MAX_LOG_VALUE_CHARS = 512


class SecurityEngine(SecurityPort):
    """Staged security checks for input, tool calls, and optional output."""

    def __init__(self, config: SecurityConfig):
        self._config = config

    def check_input(self, event_text: str, context: dict[str, object] | None = None) -> SecurityResult:
        if not self._config.enabled or not self._config.stages.input:
            return self._allow(stage="input", reason="stage_disabled")
        try:
            decision = decide_input(normalize_text(event_text))
            result = SecurityResult(stage="input", decision=decision)
            self._log(result, context)
            return result
        except Exception as e:
            return self._failure(stage="input", error=e, context=context)

    def check_tool(
        self,
        tool_name: str,
        args: dict[str, object],
        context: dict[str, object] | None = None,
    ) -> SecurityResult:
        if not self._config.enabled or not self._config.stages.tool:
            return self._allow(stage="tool", reason="stage_disabled")
        try:
            decision = decide_tool(tool_name, args)
            result = SecurityResult(stage="tool", decision=decision)
            self._log(result, context)
            return result
        except Exception as e:
            return self._failure(stage="tool", error=e, context=context)

    def check_output(self, text: str, context: dict[str, object] | None = None) -> SecurityResult:
        if not self._config.enabled or not self._config.stages.output:
            return self._allow(stage="output", reason="stage_disabled")
        try:
            decision, sanitized = decide_output(text, redact_placeholder=self._config.redact_placeholder)
            result = SecurityResult(stage="output", decision=decision, sanitized_text=sanitized)
            self._log(result, context)
            return result
        except Exception as e:
            return self._failure(stage="output", error=e, context=context)

    def _allow(self, *, stage: SecurityStage, reason: str) -> SecurityResult:
        return SecurityResult(stage=stage, decision=SecurityDecision(action="allow", reason=reason))

    def _failure(
        self,
        *,
        stage: SecurityStage,
        error: Exception,
        context: dict[str, object] | None,
    ) -> SecurityResult:
        safe_context = self._sanitize_context(context)
        logger.warning(
            "security_error stage={} fail_mode={} error={} context={}",
            stage,
            self._config.fail_mode,
            error,
            safe_context,
        )

        if self._config.fail_mode == "open":
            return SecurityResult(
                stage=stage,
                decision=SecurityDecision(
                    action="allow",
                    reason="security_error_fail_open",
                    severity="low",
                    tags=("engine_error",),
                ),
            )

        if self._config.fail_mode == "closed":
            return SecurityResult(
                stage=stage,
                decision=SecurityDecision(
                    action="block",
                    reason="security_error_fail_closed",
                    severity="high",
                    tags=("engine_error",),
                ),
            )

        # mixed: input fail-open, tool fail-closed, output sanitize
        if stage == "input":
            return SecurityResult(
                stage=stage,
                decision=SecurityDecision(
                    action="allow",
                    reason="security_error_fail_open_input",
                    severity="low",
                    tags=("engine_error",),
                ),
            )
        if stage == "tool":
            return SecurityResult(
                stage=stage,
                decision=SecurityDecision(
                    action="block",
                    reason="security_error_fail_closed_tool",
                    severity="high",
                    tags=("engine_error",),
                ),
            )

        return SecurityResult(
            stage=stage,
            decision=SecurityDecision(
                action="sanitize",
                reason="security_error_sanitize_output",
                severity="high",
                tags=("engine_error",),
            ),
            sanitized_text=self._config.block_user_message,
        )

    @staticmethod
    def _log(result: SecurityResult, context: dict[str, object] | None) -> None:
        if result.decision.action == "allow":
            return
        safe_context = SecurityEngine._sanitize_context(context)
        logger.info(
            "security_decision stage={} action={} severity={} reason={} tags={} context={}",
            result.stage,
            result.decision.action,
            result.decision.severity,
            result.decision.reason,
            list(result.decision.tags),
            safe_context,
        )

    @staticmethod
    def _sanitize_context(context: dict[str, object] | None) -> dict[str, object]:
        if not context:
            return {}
        return {
            str(key): SecurityEngine._sanitize_value(value, parent_key=str(key))
            for key, value in context.items()
        }

    @staticmethod
    def _sanitize_value(value: object, *, parent_key: str = "") -> object:
        lowered_key = parent_key.lower()
        if any(token in lowered_key for token in _SENSITIVE_CONTEXT_KEYS):
            return "[REDACTED]"

        if isinstance(value, dict):
            return {
                str(k): SecurityEngine._sanitize_value(v, parent_key=str(k))
                for k, v in value.items()
            }

        if isinstance(value, list):
            return [SecurityEngine._sanitize_value(item, parent_key=parent_key) for item in value]

        if isinstance(value, tuple):
            return tuple(SecurityEngine._sanitize_value(item, parent_key=parent_key) for item in value)

        if isinstance(value, str):
            return SecurityEngine._sanitize_string(value)

        return value

    @staticmethod
    def _sanitize_string(text: str) -> str:
        sanitized = text
        for pattern in _SENSITIVE_VALUE_PATTERNS:
            sanitized = pattern.sub("[REDACTED]", sanitized)
        if len(sanitized) > _MAX_LOG_VALUE_CHARS:
            return sanitized[:_MAX_LOG_VALUE_CHARS] + "...(truncated)"
        return sanitized
