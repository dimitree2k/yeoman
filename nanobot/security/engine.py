"""Nanobot-native security middleware engine."""

from __future__ import annotations

from loguru import logger

from nanobot.config.schema import SecurityConfig
from nanobot.core.models import SecurityDecision, SecurityResult, SecurityStage
from nanobot.core.ports import SecurityPort
from nanobot.security.normalize import normalize_text
from nanobot.security.rules import decide_input, decide_output, decide_tool


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
        logger.warning(
            "security_error stage={} fail_mode={} error={} context={}",
            stage,
            self._config.fail_mode,
            error,
            context or {},
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
        logger.info(
            "security_decision stage={} action={} severity={} reason={} tags={} context={}",
            result.stage,
            result.decision.action,
            result.decision.severity,
            result.decision.reason,
            list(result.decision.tags),
            context or {},
        )
