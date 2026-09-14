"""Small policy-backed capability cooldowns for expensive human tool calls."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from yeoman_gateway.processing.models import now_ms as _now_ms
from yeoman_gateway.processing.store import ProcessingStore
from yeoman_gateway.processing.tool_context import ToolInvocationContext, current_tool_context


def quota_key_for(tool_name: str, arguments: dict[str, Any]) -> str | None:
    """Map validated execution targets to their one explicit quota."""
    if tool_name == "deep_research":
        return "deep_research"
    if tool_name == "a2a_delegate":
        return {
            "research.deep": "deep_research",
            "trading.analyze": "trading_guru",
        }.get(arguments.get("skill"))
    return None


@dataclass(frozen=True, slots=True)
class CapabilityQuotaDecision:
    allowed: bool
    quota_key: str | None = None
    canonical_user_id: str = ""
    claim_id: str = ""
    reason: str = ""
    retry_at_ms: int = 0


class CapabilityQuotaGovernance:
    """Apply current policy and atomically book one protected capability."""

    def __init__(
        self,
        *,
        store: ProcessingStore | None,
        policy_provider: Any | None,
        clock: Any = _now_ms,
    ) -> None:
        self._store = store
        self._policy_provider = policy_provider
        self._clock = clock

    def claim(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        context: ToolInvocationContext | None = None,
    ) -> CapabilityQuotaDecision:
        key = quota_key_for(tool_name, arguments)
        if key is None:
            return CapabilityQuotaDecision(allowed=True)

        context = context or current_tool_context()
        if context is None:
            return CapabilityQuotaDecision(allowed=False, quota_key=key, reason="identity_unresolved")
        if context.channel != "whatsapp" or context.is_owner:
            return CapabilityQuotaDecision(allowed=True, quota_key=key)
        if not context.canonical_user_id:
            return CapabilityQuotaDecision(allowed=False, quota_key=key, reason="identity_unresolved")
        if self._store is None or self._policy_provider is None:
            return CapabilityQuotaDecision(allowed=False, quota_key=key, reason="quota_unavailable")

        try:
            provider = getattr(self._policy_provider, "current_policy_snapshot", None)
            if not callable(provider):
                provider = getattr(self._policy_provider, "policy_snapshot", None)
            if not callable(provider):
                provider = getattr(self._policy_provider, "snapshot", None)
            if not callable(provider):
                raise RuntimeError("policy provider has no snapshot method")
            snapshot = provider()
            if not snapshot.healthy or snapshot.policy is None:
                return CapabilityQuotaDecision(allowed=False, quota_key=key, reason="quota_unavailable")
            quotas = getattr(snapshot.policy, "capability_quotas", None)
            if quotas is None and isinstance(snapshot.policy, dict):
                quotas = snapshot.policy.get("capabilityQuotas", {})
            if quotas is None:
                return CapabilityQuotaDecision(
                    allowed=False, quota_key=key, reason="quota_unavailable"
                )
            config = quotas.get(key) if isinstance(quotas, dict) else None
            if config is None:
                return CapabilityQuotaDecision(allowed=True, quota_key=key)
            cooldown_seconds = int(
                config.get("cooldownSeconds", config.get("cooldown_seconds", 0))
                if isinstance(config, dict)
                else config.cooldown_seconds
            )
            claim_id = uuid.uuid4().hex
            allowed, retry_at_ms = self._store.claim_capability(
                context.canonical_user_id,
                key,
                claim_id,
                cooldown_ms=cooldown_seconds * 1000,
                now_ms=int(self._clock()),
            )
        except Exception:
            return CapabilityQuotaDecision(allowed=False, quota_key=key, reason="quota_unavailable")
        if not allowed:
            return CapabilityQuotaDecision(
                allowed=False,
                quota_key=key,
                canonical_user_id=context.canonical_user_id,
                reason="quota_exhausted",
                retry_at_ms=retry_at_ms,
            )
        return CapabilityQuotaDecision(
            allowed=True,
            quota_key=key,
            canonical_user_id=context.canonical_user_id,
            claim_id=claim_id,
        )

    def release(self, decision: CapabilityQuotaDecision) -> bool:
        if not decision.claim_id or not decision.quota_key or not decision.canonical_user_id:
            return False
        if self._store is None:
            return False
        try:
            return self._store.release_capability(
                decision.canonical_user_id, decision.quota_key, decision.claim_id
            )
        except Exception:
            return False

    @staticmethod
    def refusal(capability: str, decision: CapabilityQuotaDecision) -> str:
        suffix = (
            f" retry_at_ms={decision.retry_at_ms}"
            if decision.reason == "quota_exhausted"
            else ""
        )
        return f"Error: {capability} {decision.reason}{suffix}"


__all__ = ["CapabilityQuotaDecision", "CapabilityQuotaGovernance", "quota_key_for"]
