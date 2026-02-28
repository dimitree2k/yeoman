"""No-op security implementation."""

from __future__ import annotations

from yeoman.core.models import SecurityDecision, SecurityResult
from yeoman.core.ports import SecurityPort


class NoopSecurity(SecurityPort):
    """SecurityPort implementation that allows everything."""

    def check_input(self, event_text: str, context: dict[str, object] | None = None) -> SecurityResult:
        del event_text, context
        return SecurityResult(stage="input", decision=SecurityDecision(action="allow", reason="security_disabled"))

    def check_tool(
        self,
        tool_name: str,
        args: dict[str, object],
        context: dict[str, object] | None = None,
    ) -> SecurityResult:
        del tool_name, args, context
        return SecurityResult(stage="tool", decision=SecurityDecision(action="allow", reason="security_disabled"))

    def check_output(self, text: str, context: dict[str, object] | None = None) -> SecurityResult:
        del text, context
        return SecurityResult(stage="output", decision=SecurityDecision(action="allow", reason="security_disabled"))
