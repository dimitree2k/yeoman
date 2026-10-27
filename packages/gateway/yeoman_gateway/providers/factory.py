"""Factory helpers for task-specific provider construction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from yeoman_gateway.providers.litellm_provider import LiteLLMProvider
from yeoman_gateway.providers.registry import find_by_model, find_by_name

if TYPE_CHECKING:
    from yeoman_shared.config.schema import Config

    from yeoman_gateway.providers.base import LLMProvider


@dataclass(frozen=True, slots=True)
class ProviderCredentials:
    """Connection settings of one configured provider."""

    api_key: str | None
    api_base: str | None
    extra_headers: dict[str, str] | None
    provider_name: str | None


@dataclass(slots=True)
class ProviderFactory:
    """Build scoped provider instances for routed task models."""

    config: "Config"

    def create_chat_provider(
        self, model: str, provider_name: str | None = None
    ) -> "LLMProvider":
        """Create a provider bound to the supplied model route."""
        credentials = self.credentials(model, provider_name)
        return LiteLLMProvider(
            api_key=credentials.api_key,
            api_base=credentials.api_base,
            default_model=model,
            extra_headers=credentials.extra_headers,
            provider_name=credentials.provider_name,
        )

    def credentials(self, model: str, provider_name: str | None = None) -> ProviderCredentials:
        """Resolve key, base URL and headers for a routed model without building a client."""
        provider_cfg = self.config.get_provider(model, provider_name=provider_name)
        provider_spec = find_by_name(provider_name) if provider_name else find_by_model(model)
        api_key = provider_cfg.api_key if provider_cfg and provider_cfg.api_key else None
        api_base = provider_cfg.api_base if provider_cfg else None
        if not api_base and provider_spec and provider_spec.default_api_base:
            api_base = provider_spec.default_api_base
        extra_headers = provider_cfg.extra_headers if provider_cfg else None
        effective_provider = provider_name or next(
            (name for name, value in self.config.providers if value is provider_cfg),
            None,
        )
        return ProviderCredentials(
            api_key=api_key,
            api_base=api_base,
            extra_headers=extra_headers,
            provider_name=effective_provider,
        )
