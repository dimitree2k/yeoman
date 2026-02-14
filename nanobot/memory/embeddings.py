"""Embedding client for memory using LiteLLM."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger

from nanobot.providers.litellm_provider import LiteLLMProvider

if TYPE_CHECKING:
    from nanobot.config.schema import Config


class MemoryEmbeddingService:
    """Resolve embedding route and fetch vectors via LiteLLM."""

    def __init__(self, *, config: "Config", route_key: str) -> None:
        self._config = config
        self._route_key = route_key
        self._model = self._resolve_model()
        self._provider = self._create_provider(self._model)

    @property
    def model(self) -> str:
        return self._model

    def _resolve_model(self) -> str:
        route_name = self._config.models.routes.get(self._route_key)
        if not route_name:
            raise ValueError(f"models.routes missing '{self._route_key}'")
        profile = self._config.models.profiles.get(route_name)
        if profile is None:
            raise ValueError(
                f"models.routes['{self._route_key}'] points to missing profile '{route_name}'"
            )
        if profile.kind != "embedding":
            raise ValueError(
                f"route '{self._route_key}' must target kind='embedding', got '{profile.kind}'"
            )
        model = (profile.model or "").strip()
        if not model:
            raise ValueError(f"profile '{route_name}' does not define a model")
        return model

    def _create_provider(self, model: str) -> LiteLLMProvider:
        provider_cfg = self._config.get_provider(model)
        api_key = provider_cfg.api_key if provider_cfg and provider_cfg.api_key else None
        api_base = self._config.get_api_base(model)
        extra_headers = provider_cfg.extra_headers if provider_cfg else None
        return LiteLLMProvider(
            api_key=api_key,
            api_base=api_base,
            default_model=model,
            extra_headers=extra_headers,
        )

    def embed(self, text: str) -> list[float] | None:
        compact = " ".join(text.split()).strip()
        if not compact:
            return None

        try:
            from litellm import embedding

            model = self._provider._resolve_model(self._model)
            kwargs: dict[str, Any] = {
                "model": model,
                "input": [compact],
            }
            if self._provider.api_base:
                kwargs["api_base"] = self._provider.api_base
            if self._provider.extra_headers:
                kwargs["extra_headers"] = self._provider.extra_headers
            response = embedding(**kwargs)
            data = getattr(response, "data", None)
            if not data:
                return None
            vector = data[0].get("embedding") if isinstance(data[0], dict) else None
            if vector is None:
                vector = getattr(data[0], "embedding", None)
            if not isinstance(vector, list):
                return None
            return [float(v) for v in vector]
        except Exception as exc:
            logger.debug("memory embedding failed: {}", exc)
            return None
