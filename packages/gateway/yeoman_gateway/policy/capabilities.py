"""Policy names for capabilities implemented outside the LLM tool registry."""

from __future__ import annotations

SERVICE_POLICY_CAPABILITIES = frozenset({"send_media"})


def policy_known_tools(tool_names: set[str]) -> set[str]:
    """Return policy vocabulary, including service-only capabilities."""
    return set(tool_names) | SERVICE_POLICY_CAPABILITIES
