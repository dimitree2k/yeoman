"""Embedding client for memory using LiteLLM."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger

from yeoman_gateway.providers.litellm_provider import LiteLLMProvider

if TYPE_CHECKING:
    from yeoman_shared.config.schema import Config, ModelProfile


class MemoryEmbeddingService:
    """Resolve embedding route and fetch vectors via LiteLLM."""

    def __init__(self, *, config: "Config", route_key: str) -> None:
        self._config = config
        self._route_key = route_key
        self._profile = self._resolve_profile()
        self._model = (self._profile.model or "").strip()
        self._provider = self._create_provider(self._model, self._profile.provider)

    @property
    def model(self) -> str:
        return self._model

    def _resolve_profile(self) -> "ModelProfile":
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
        if not (profile.model or "").strip():
            raise ValueError(f"profile '{route_name}' does not define a model")
        return profile

    def _create_provider(self, model: str, provider_name: str | None) -> LiteLLMProvider:
        provider_cfg = self._config.get_provider(model, provider_name=provider_name)
        if provider_cfg is None:
            raise ValueError(
                f"no provider with credentials for embedding route '{self._route_key}' "
                f"(model={model!r}, provider={provider_name or 'auto'!r})"
            )
        api_key = provider_cfg.api_key if provider_cfg.api_key else None
        api_base = provider_cfg.api_base
        extra_headers = provider_cfg.extra_headers
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
            # encoding_format="float" is required by OpenRouter's /v1/embeddings
            # schema (Zod-validated; rejects the call when missing). OpenAI's
            # native endpoint accepts it as well, so this is safe across gateways.
            kwargs: dict[str, Any] = {
                "model": model,
                "input": [compact],
                "encoding_format": "float",
            }
            if self._provider.api_key:
                kwargs["api_key"] = self._provider.api_key
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
