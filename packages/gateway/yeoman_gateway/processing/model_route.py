"""One cheap model call on a configured route, shared by the small decisions.

Both the reaction choice and the ambient verdict are the same shape: a terse prompt, a
strictly bounded answer, and a route that must stay cheap. The provider plumbing lives
here so neither caller has to grow its own copy of it.
"""

from __future__ import annotations

from typing import Any


class RouteUnavailableError(RuntimeError):
    """The configured route cannot be used (missing route, profile or credentials)."""


class RouteClient:
    """A resolved chat route: provider, model and profile limits."""

    def __init__(self, *, config: Any, route_key: str) -> None:
        from yeoman_gateway.providers.litellm_provider import LiteLLMProvider

        self.route_key = str(route_key)
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
        self._provider = LiteLLMProvider(
            api_key=provider_cfg.api_key if provider_cfg.api_key else None,
            api_base=provider_cfg.api_base,
            default_model=model,
            extra_headers=provider_cfg.extra_headers,
        )

    async def chat(self, messages: list[dict[str, str]], *, max_tokens: int) -> str:
        """One completion, deterministically, with the caller's token ceiling."""
        response = await self._provider.chat(
            messages=messages,
            tools=None,
            model=self.model,
            max_tokens=max_tokens,
            temperature=0.0,
        )
        return str(getattr(response, "content", "") or "")


def resolve_route_key(config: Any, *candidates: str) -> str:
    """The first configured route of ``candidates``, or the last one as a last resort."""
    routes = getattr(getattr(config, "models", None), "routes", None) or {}
    for key in candidates:
        name = str(key or "").strip()
        if name and name in routes:
            return name
    return str(candidates[-1] or "")


__all__ = ["RouteClient", "RouteUnavailableError", "resolve_route_key"]
