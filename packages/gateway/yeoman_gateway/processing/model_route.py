"""One cheap model call on a configured route, shared by the small decisions.

Both the reaction choice and the ambient verdict are the same shape: a terse prompt, a
strictly bounded answer, and a route that must stay cheap. The provider plumbing lives
here so neither caller has to grow its own copy of it.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from loguru import logger


@dataclass(frozen=True, slots=True)
class RouteReply:
    """One completion with the evidence needed to talk about its cost."""

    content: str
    usage: Mapping[str, int] = field(default_factory=dict)
    model: str = ""
    latency_ms: int = 0
    finish_reason: str = ""
    diagnostics: Mapping[str, Any] = field(default_factory=dict)
    fallback_used: bool = False


class RouteUnavailableError(RuntimeError):
    """The configured route cannot be used (missing route, profile or credentials)."""


class RouteCallError(RuntimeError):
    """Every attempt on a route failed; ``reason`` is a stable failure code."""

    def __init__(self, reason: str) -> None:
        if reason not in {"timeout", "provider_error"}:
            reason = "provider_error"
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class _RouteTarget:
    """One resolved profile: everything needed for one provider attempt."""

    profile_name: str
    model: str
    timeout_ms: int
    reasoning: dict[str, Any] | None
    provider: Any


def _make_provider(model: str, provider_cfg: Any) -> Any:
    from yeoman_gateway.providers.litellm_provider import LiteLLMProvider

    return LiteLLMProvider(
        api_key=provider_cfg.api_key if provider_cfg.api_key else None,
        api_base=provider_cfg.api_base,
        default_model=model,
        extra_headers=provider_cfg.extra_headers,
    )


class RouteClient:
    """A resolved chat route: provider, model and profile limits."""

    def __init__(self, *, config: Any, route_key: str) -> None:
        self.route_key = str(route_key)
        self._config = config
        models = getattr(config, "models", None)
        routes = getattr(models, "routes", None) or {}
        route_name = routes.get(self.route_key)
        if not route_name:
            raise RouteUnavailableError(f"models.routes missing '{self.route_key}'")
        profile = (getattr(models, "profiles", None) or {}).get(route_name)
        if profile is None:
            raise RouteUnavailableError(
                f"models.routes['{self.route_key}'] points to missing profile '{route_name}'"
            )
        model = str(getattr(profile, "model", "") or "").strip()
        if not model:
            raise RouteUnavailableError(f"profile '{route_name}' does not define a model")
        provider_cfg = config.get_provider(model, provider_name=getattr(profile, "provider", None))
        if provider_cfg is None:
            raise RouteUnavailableError(
                f"no provider with credentials for route '{self.route_key}'"
            )
        self.model = model
        self.timeout_ms = int(getattr(profile, "timeout_ms", 0) or 0)
        self.structured_output = str(
            getattr(profile, "structured_output", "json_schema_strict") or "json_schema_strict"
        )
        reasoning = getattr(profile, "reasoning", None)
        self.reasoning: dict[str, Any] | None = dict(reasoning) if reasoning else None
        self._provider = _make_provider(model, provider_cfg)
        self._primary = _RouteTarget(
            profile_name=str(route_name),
            model=model,
            timeout_ms=self.timeout_ms,
            reasoning=self.reasoning,
            provider=self._provider,
        )
        self._fallback_names = tuple(
            str(name).strip()
            for name in (getattr(profile, "fallback", None) or ())
            if str(name).strip()
        )

    def _resolve_fallback(self, profile_name: str) -> _RouteTarget | None:
        """Resolve one fallback profile like the primary; an unknown name is skipped."""
        try:
            config = getattr(self, "_config", None)
            profiles = getattr(getattr(config, "models", None), "profiles", None) or {}
            profile = profiles.get(profile_name)
            model = (
                str(getattr(profile, "model", "") or "").strip()
                if profile is not None
                else ""
            )
            provider_cfg = (
                config.get_provider(model, provider_name=getattr(profile, "provider", None))
                if profile is not None and model
                else None
            )
            if profile is None or not model or provider_cfg is None:
                raise RouteUnavailableError(
                    f"fallback profile '{profile_name}' does not resolve"
                )
            provider = _make_provider(model, provider_cfg)
            reasoning = getattr(profile, "reasoning", None)
            return _RouteTarget(
                profile_name=profile_name,
                model=model,
                timeout_ms=int(getattr(profile, "timeout_ms", 0) or 0),
                reasoning=dict(reasoning) if reasoning else None,
                provider=provider,
            )
        except Exception as exc:  # noqa: BLE001 - a bad fallback never breaks the route
            logger.warning(
                "route_fallback_unavailable route={} profile={}",
                self.route_key,
                profile_name,
            )
            logger.debug(
                "route_fallback_resolution_failed route={} profile={} error_type={}",
                self.route_key,
                profile_name,
                type(exc).__name__,
            )
            return None

    def _attempt_targets(self) -> list[_RouteTarget]:
        primary = getattr(self, "_primary", None)
        if primary is None:
            primary = _RouteTarget(
                profile_name="",
                model=self.model,
                timeout_ms=self.timeout_ms,
                reasoning=self.reasoning,
                provider=self._provider,
            )
        targets = [primary]
        for name in getattr(self, "_fallback_names", ()):
            target = self._resolve_fallback(name)
            if target is not None:
                targets.append(target)
        return targets

    @staticmethod
    async def _call_target(
        target: _RouteTarget,
        *,
        messages: list[dict[str, str]],
        max_tokens: int,
        response_format: dict[str, Any] | None,
        max_retries: int | None,
        timeout_ms: int | None = None,
    ) -> Any:
        call = target.provider.chat(
            messages=messages,
            tools=None,
            model=target.model,
            max_tokens=max_tokens,
            temperature=0.0,
            reasoning=target.reasoning,
            response_format=response_format,
            max_retries=max_retries,
        )
        limit_ms = target.timeout_ms if timeout_ms is None else int(timeout_ms)
        if limit_ms > 0:
            return await asyncio.wait_for(call, timeout=limit_ms / 1000)
        return await call

    async def chat_with_usage(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        response_format: dict[str, Any] | None = None,
        max_retries: int | None = None,
        timeout_ms: int | None = None,
    ) -> RouteReply:
        """Completion plus provider usage/status; None preserves the global retry default.

        Each configured fallback is tried once, in order, after a timeout or a
        provider error. When every attempt fails, the last failure is raised as
        :class:`RouteCallError`; cancellation always propagates untouched.

        ``timeout_ms`` replaces the profile's per-attempt timeout for this call: a
        caller with its own, longer budget (the weekly digest shares the judge's
        profile) must not be cut short by the profile meant for another caller.
        """
        started = time.monotonic()
        last_error: RouteCallError | None = None
        for index, target in enumerate(self._attempt_targets()):
            try:
                response = await self._call_target(
                    target,
                    messages=messages,
                    max_tokens=max_tokens,
                    response_format=response_format,
                    max_retries=max_retries,
                    timeout_ms=timeout_ms,
                )
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                last_error = RouteCallError("timeout")
                logger.debug(
                    "route_attempt_failed route={} profile={} reason=timeout",
                    self.route_key,
                    target.profile_name,
                )
                continue
            except Exception as exc:
                last_error = RouteCallError("provider_error")
                logger.debug(
                    "route_attempt_failed route={} profile={} reason=provider_error "
                    "error_type={}",
                    self.route_key,
                    target.profile_name,
                    type(exc).__name__,
                )
                continue
            finish_reason = str(getattr(response, "finish_reason", "") or "")
            if finish_reason == "error":
                last_error = RouteCallError("provider_error")
                logger.debug(
                    "route_attempt_failed route={} profile={} reason=provider_error",
                    self.route_key,
                    target.profile_name,
                )
                continue
            raw_usage = getattr(response, "usage", None) or {}
            usage = {
                str(key): int(value)
                for key, value in dict(raw_usage).items()
                if isinstance(value, int) and not isinstance(value, bool)
            }
            return RouteReply(
                content=str(getattr(response, "content", "") or ""),
                usage=usage,
                model=target.model,
                latency_ms=int((time.monotonic() - started) * 1000),
                finish_reason=finish_reason,
                diagnostics=getattr(response, "diagnostics", None) or {},
                fallback_used=index > 0,
            )
        raise last_error or RouteCallError("provider_error")

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        """One completion, deterministically, with the caller's token ceiling."""
        reply = await self.chat_with_usage(
            messages, max_tokens=max_tokens, response_format=response_format
        )
        return reply.content


def resolve_route_key(config: Any, *candidates: str) -> str:
    """The first configured route of ``candidates``, or the last one as a last resort."""
    routes = getattr(getattr(config, "models", None), "routes", None) or {}
    for key in candidates:
        name = str(key or "").strip()
        if name and name in routes:
            return name
    return str(candidates[-1] or "")


__all__ = [
    "RouteCallError",
    "RouteClient",
    "RouteReply",
    "RouteUnavailableError",
    "resolve_route_key",
]
